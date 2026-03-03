# Copyright 2020 ForgeFlow S.L.
# Copyright 2019 Aleph Objects, Inc.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).


from odoo import api, fields, models
from odoo.tools import float_compare, float_is_zero


class ProductProduct(models.Model):
    _inherit = "product.product"

    account_value = fields.Monetary(
        "Accounting Value",
        compute="_compute_inventory_value",
        currency_field="company_currency_id",
    )
    qty_at_date = fields.Float(
        "Inventory Quantity",
        compute="_compute_inventory_value",
        digits="Product Unit",
    )
    account_qty_at_date = fields.Float(
        "Accounting Quantity",
        compute="_compute_inventory_value",
        digits="Product Unit",
    )
    valuation_discrepancy = fields.Monetary(
        compute="_compute_inventory_value",
        currency_field="company_currency_id",
        search="_search_valuation_discrepancy",
    )
    qty_discrepancy = fields.Float(
        compute="_compute_inventory_value",
        search="_search_qty_discrepancy",
        digits="Product Unit",
    )

    @api.model
    def _get_storable_products_with_moves(self):
        """Storable products that have done stock moves.
        Products outside this set can't have valuation discrepancies."""
        to_date = self.env.context.get("to_date", False)
        params = {"company_id": self.env.company.id}
        date_clause = ""
        if to_date:
            date_clause = "AND sm.date <= %(to_date)s"
            params["to_date"] = to_date
        # pylint: disable=E8103
        self.env.cr.execute(
            f"""
            SELECT DISTINCT sm.product_id
            FROM stock_move sm
            JOIN product_product pp ON pp.id = sm.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE sm.state = 'done'
            AND sm.company_id = %(company_id)s
            AND pt.is_storable = true
            {date_clause}
            """,
            params,
        )
        product_ids = [r[0] for r in self.env.cr.fetchall()]
        return self.with_context(active_test=False).browse(product_ids)

    @api.model
    def _get_valuation_account_ids(self):
        """All stock valuation account IDs for the current company.

        There are typically very few product categories, so loading all
        is intentional and cheap.
        """
        categories = (
            self.env["product.category"]
            .with_context(active_test=False)
            .search([("property_stock_valuation_account_id", "!=", False)])
        )
        account_ids = set(categories.mapped("property_stock_valuation_account_id").ids)
        fallback = self.env.company.account_stock_valuation_id
        if fallback:
            account_ids.add(fallback.id)
        return account_ids

    @api.model
    def _get_accounting_values_by_product(self, product_ids):
        """Return {product_id: (balance, qty)} for valuation accounts via SQL."""
        if not product_ids:
            return {}
        to_date = self.env.context.get("to_date", False)
        company = self.env.company
        valuation_account_ids = self._get_valuation_account_ids()
        if not valuation_account_ids:
            return {}
        where_date = "AND aml.date <= %s" if to_date else ""
        # pylint: disable=E8103
        self.env.cr.execute(
            f"""
            SELECT aml.product_id,
                sum(aml.balance),
                sum(aml.quantity)
            FROM account_move_line AS aml
            WHERE aml.product_id IN %s
            AND aml.account_id IN %s
            AND aml.parent_state = 'posted'
            AND aml.company_id = %s
            {where_date}
            GROUP BY aml.product_id
            """,
            (tuple(product_ids), tuple(valuation_account_ids), company.id)
            + ((to_date,) if to_date else ()),
        )
        return {row[0]: (row[1], row[2]) for row in self.env.cr.fetchall()}

    @api.model
    def _search_qty_discrepancy(self, operator, value):
        products = self._get_storable_products_with_moves()
        if not products:
            return [("id", "in", [])]
        acct = self._get_accounting_values_by_product(products.ids)
        products_valued = products._with_valuation_context()
        qty_data = {p.id: p.qty_available for p in products_valued}
        result_ids = []
        for p in products:
            if p.valuation != "real_time":
                continue
            _, acct_qty = acct.get(p.id, (0, 0))
            stock_qty = qty_data.get(p.id, 0)
            if float_compare(stock_qty, acct_qty, precision_rounding=p.uom_id.rounding):
                result_ids.append(p.id)
        return [("id", "in", result_ids)]

    @api.model
    def _search_valuation_discrepancy(self, operator, value):
        products = self._get_storable_products_with_moves()
        if not products:
            return [("id", "in", [])]
        company = self.env.company
        rounding = company.currency_id.rounding
        acct = self._get_accounting_values_by_product(products.ids)
        products_with_acct = {
            pid
            for pid, (balance, _) in acct.items()
            if not float_is_zero(balance, precision_rounding=rounding)
        }
        products_valued = products._with_valuation_context()
        qty_data = {}
        products_with_stock = set()
        for p in products_valued:
            qty = p.qty_available
            qty_data[p.id] = qty
            if not p.uom_id.is_zero(qty):
                products_with_stock.add(p.id)
        candidate_ids = products_with_acct | products_with_stock
        if not candidate_ids:
            return [("id", "in", [])]
        candidates = self.with_context(active_test=False).browse(candidate_ids)
        result_ids = []
        for p in candidates:
            if p.valuation != "real_time":
                continue
            acct_val = acct.get(p.id, (0, 0))[0]
            if p.cost_method == "fifo":
                stock_val = p.total_value
            else:
                stock_val = qty_data.get(p.id, 0) * p.standard_price
            if not float_is_zero(stock_val - acct_val, precision_rounding=rounding):
                result_ids.append(p.id)
        return [("id", "in", result_ids)]

    def _compute_inventory_value(self):
        self.env["account.move.line"].check_access("read")
        if not self:
            return
        acct = self._get_accounting_values_by_product(self.ids)
        products_valued = self._with_valuation_context()
        qty_data = {p.id: p.qty_available for p in products_valued}

        for product in self:
            if product.valuation == "real_time":
                value, qty = acct.get(product.id, (0, 0))
                product.account_value = value
                product.account_qty_at_date = qty
            else:
                product.account_value = 0
                product.account_qty_at_date = 0

            product.qty_at_date = qty_data.get(product.id, 0)
            if product.valuation == "real_time":
                product.valuation_discrepancy = (
                    product.total_value - product.account_value
                )
                product.qty_discrepancy = (
                    product.qty_at_date - product.account_qty_at_date
                )
            else:
                product.valuation_discrepancy = 0
                product.qty_discrepancy = 0

    def _get_move_domain(self):
        self.ensure_one()
        to_date = self.env.context.get("to_date", False)
        domain = [
            ("product_id", "=", self.id),
            ("company_id", "=", self.env.company.id),
            ("state", "=", "done"),
        ]
        if to_date:
            domain.append(("date", "<=", to_date))
        return domain

    def action_view_amls(self):
        self.ensure_one()
        to_date = self.env.context.get("to_date", False)
        valuation_account_id = self.categ_id.property_stock_valuation_account_id.id
        domain = [
            ("product_id", "=", self.id),
            ("account_id", "=", valuation_account_id),
            ("parent_state", "=", "posted"),
            ("company_id", "=", self.env.company.id),
        ]
        if to_date:
            domain.append(("date", "<=", to_date))
        list_view_ref = self.env.ref("account.view_move_line_tree")
        form_view_ref = self.env.ref("account.view_move_line_form")
        return {
            "name": self.env._("Accounting Valuation at date"),
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "context": self.env.context,
            "res_model": "account.move.line",
            "domain": domain,
            "views": [(list_view_ref.id, "list"), (form_view_ref.id, "form")],
        }

    def action_view_valuation_layers(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "stock_account.stock_valuation_layer_report_action"
        )
        action["domain"] = self._get_move_domain()
        action["context"] = {}
        return action

    def action_view_valuation_moves(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("stock.stock_move_action")
        action["domain"] = self._get_move_domain()
        return action
