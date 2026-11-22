{
    'name': 'Stock Rebalancing Lite',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Find stock imbalances between two warehouses and get explained '
               'transfer recommendations. Lite: 1 company, 2 warehouses and 50 '
               'products per run, 3 analyses in total, top 10 recommendations.',
    'description': """
Stock Rebalancing Lite
======================
Move inventory smarter. Buy only what you need.

* Demand per warehouse from done customer deliveries over a look-back period.
* On hand, reserved and available stock per product and warehouse.
* Coverage days and status (shortage / balanced / excess).
* Explained transfer recommendations that protect the donor warehouse's
  minimum coverage and never move reserved stock.
* Estimated avoided procurement value (labelled estimate).

Lite edition limits (disclosed)
-------------------------------
* One company.
* Up to 2 warehouses per analysis run.
* Up to 50 products per analysis run.
* Up to 3 completed analysis runs in total (past runs stay viewable).
* Up to 10 recommendations shown per run (the number of additional
  recommendations found is shown).
* Recommendations are read-only: draft transfer creation, lot/expiry (FEFO)
  awareness, constraints, before/after simulation, excess redistribution,
  scheduled analysis, export of results and multi-company are part of the
  Pro edition.

A Pro edition is available. Support: info@elaf-development.com
""",
    'author': 'Elaf Development',
    'website': 'https://www.elaf-development.com/',
    'support': 'info@elaf-development.com',
    'license': 'LGPL-3',
    'depends': ['stock', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/stat_views.xml',
        'views/line_views.xml',
        'views/run_views.xml',
        'views/usage_views.xml',
        'views/dashboard_action.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'elaf_stock_rebalance_lite/static/src/scss/rebalance.scss',
            'elaf_stock_rebalance_lite/static/src/dashboard/*.js',
            'elaf_stock_rebalance_lite/static/src/dashboard/*.xml',
        ],
    },
    'images': ['static/description/banner.png', 'static/description/main_screenshot.png'],
    'application': True,
    'installable': True,
}
