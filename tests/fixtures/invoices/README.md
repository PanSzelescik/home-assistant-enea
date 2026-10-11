# Invoice regressions

`issue19_g11.json` preserves the billing structure reported in
[issue #19](https://github.com/PanSzelescik/home-assistant-enea/issues/19#issuecomment-6110138268):
G11, three phases, annual billing, a 0.8 opust, a partial December and the
capacity-fee suspension through June 2025. Energy quantities have been scaled
and the individual trade fee changed. No PPE, meter ID, invoice number,
address, account data or diagnostic export is included.

The rows are synthetic aggregate energy volumes placed in representative
hours, not the customer's hourly load profile. Prices are constant for G11
throughout this period. Keep the calendar year: moving it would change the
regulated rates and the suspension boundary being tested.

Expected values are independent invoice arithmetic: billed kWh = consumed -
returned * 0.8; energy/network/quality use billed kWh, OZE/cogeneration use
all consumed kWh. Network/transition use 11 + 12/31 months; capacity uses
5 + 12/31 months. Subscription/trade use 12 months. Each component is rounded
to cents, then VAT is applied to the sum. The estimator retains precise kWh;
an invoice with integer kWh and intermediate line rounding may differ by cents.
