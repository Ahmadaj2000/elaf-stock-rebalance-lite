======================
Stock Rebalancing Lite
======================

Find stock imbalances between two warehouses and get explained transfer recommendations. The
recommendations protect the donor warehouse's minimum coverage, never move reserved stock, and
come with an avoided-procurement estimate.

.. contents:: Contents
   :local:
   :depth: 1

Overview
========

An **analysis run** compares warehouses for a set of products. You can scope it by
warehouses, product categories (including their children) or individual products.

For each product and warehouse it computes the following (stock is summed over every internal
location under the warehouse's stock location):

* **On hand** and **reserved** quantities (from stock quants).
* **Available** = on hand - reserved, and never below zero.
* **Daily demand**: done moves from the warehouse to customers within the look-back period,
  minus customer returns, divided by the look-back days.
* **Coverage days** = available / daily demand. Coverage is infinite when there is no demand.
* **Status**:

  * *Shortage*: coverage is below the target.
  * *Excess*: coverage is above the donor minimum + target. Stock with no demand also counts as
    excess.
  * *Balanced*: everything in between.

Recommendations
---------------

* The **receiver's need** is the quantity it lacks to reach the target coverage, rounded up.
* The **donor's spare** quantity is its available stock above its minimum coverage, rounded down.
* The **recommended quantity** is the smaller of the two, rounded to whole units (default on)
  and to the product's unit-of-measure rounding.
* Receivers with the lowest coverage are served first.
* A warehouse in shortage is never used as a donor.
* Reserved stock is never counted as available.

Each recommendation stores an explanation of its calculation.

**Avoided procurement (estimate)** = recommended quantity x product cost. It is shown per line,
per run and on the dashboard. It is always labelled as an estimate.

Dashboard
---------

* KPI cards: warehouses analysed, products in shortage, products in excess, recommended units
  and the avoided-procurement estimate. Each card opens its records.
* The top recommendations with their explanations.
* A coverage heat table of products by warehouse, shortages first, with up to 60 products.

Installation
============

1. Install **Stock Rebalancing Lite**. It needs Inventory and Discuss.
2. Odoo Online is not supported, because it does not run third-party Python modules.

Compatibility: Odoo 19.0 Community Edition (tested). Odoo 17.0 and 18.0 are distributed as
separate packages.

Configuration
=============

* **Users**: in *Settings > Users*, set the *Stock Rebalancing* access to **User** (this includes
  Inventory User) or **Manager**.
* **Product costs**: set them, because the avoided-procurement estimate uses the product cost.
* **Run parameters**: there is no global settings page. Parameters are set per run:

  * look-back (default 90 days),
  * target coverage (default 30 days),
  * donor minimum coverage (default 45 days; it must be at least the target),
  * whole quantities (default on).

Usage
=====

1. Open *Stock Rebalancing > Analysis > Analysis Runs* and create a run. Choose two warehouses and,
   optionally, categories or products.
2. Click **Analyse**.
3. Review the *Recommendations* and *Coverage* tabs and the smart buttons (Products, Shortages,
   Excesses, Recommendations).
4. Open a recommendation to read its explanation.
5. Create the transfers you decide on in Odoo Inventory. Lite does not create transfers.
6. **Re-run Analysis** replaces the previous results. Odoo asks you to confirm, because it counts
   as a new analysis unless the parameters and the stock data are unchanged.

Lite edition limits
===================

* One company per database: the first company that completes an analysis.
* Up to 2 warehouses per analysis run.
* Up to 50 products per run. Only products with stock or demand are counted. The limit is
  checked before anything is used.
* Up to 3 completed analysis runs in total.

  * A failed analysis does not count, and a second click within a few seconds is ignored.
    Two simultaneous requests on the same run are serialised: only one counts.
  * Re-running an analysed run is free when its parameters and the stock data it uses
    (quantities, reservations, demand, open transfers) are unchanged: the results are refreshed
    and the usage log shows *Re-analysis unchanged (not counted)*.
  * A re-run with changed parameters or data counts as a new analysis, and so does a copy or a run
    reset to draft.
  * Deleting runs does not give analyses back.
  * Past runs and recommendations stay viewable.
* The top 10 recommendations per run are shown. The number of additional recommendations found,
  with their units and estimated value, is shown on the run and on the dashboard.
* Recommendations are read-only, and no transfers are created.
* The run, coverage and recommendation lists cannot be exported.

The usage counter is shown on the dashboard and under *Stock Rebalancing > Configuration > Lite
Usage*. It is a local record in your database. There is no activation key and no remote check.

The **Discover Pro** button on *Stock Rebalancing > Lite vs Pro* opens the Pro listing on the
Odoo Apps store in your browser. It works once the Pro listing is published. No data is sent.

Security and permissions
========================

* **User**: create and analyse runs, and see the runs of their company. Users can change only
  their own runs.
* **Manager**: change all runs, open configuration and the usage log, and delete runs.
* Lite only reads stock data. It creates no transfers and changes no stock.
* Multi-company record rules apply to runs, coverage and recommendations.
* All data stays in your Odoo database. The module makes no external network calls and sends no
  e-mail.

FAQ
===

**Why is a product with stock marked excess?**
  It has no customer demand in the look-back period, so its coverage is infinite.

**Why is the recommended quantity smaller than the need?**
  The donor keeps its minimum coverage, and reserved stock is excluded. The explanation shows
  both numbers.

**Is the avoided procurement a saving?**
  It is an estimate: quantity x product cost, before transfer costs.

Known limitations
=================

* Demand is historical. Future demand is not forecast.
* Explanations are stored in the language of the user who ran the analysis.
* Not available on Odoo Online.

Support
=======

Community support, best effort: info@elaf-development.com
