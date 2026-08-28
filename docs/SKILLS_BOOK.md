# AMS ERP - SKILLS BOOK & APPLICATION MAP

**Project:** AMS (Ahmed Management System / FAZAL BUILDING MATERIALS)  
**Repository:** rehmanahmedca-source/rep-new-0001  
**Branch:** arena/01a046e8-rep-new-0001  
**Date:** 2026-08-28  
**Status:** STEP A (DISCOVERY) — ✅ COMPLETE · STEP B (DEEP QA AUDIT) — ✅ COMPLETE  

> **STEP B results:** see [`docs/STEP_B_QA_TEST_REPORT.md`](STEP_B_QA_TEST_REPORT.md)
> (machine-readable: `docs/step_b_qa_results.json`).
> 1,022 assertions · 25 full transaction cycles across 5 QA clients ·
> 140/161 GET routes exercised · **2 defects found and both now fixed** —
> the audit currently reports **0 failures, 0 open bugs**.
> Harness: `tools/qa_stepb/`. Regression locks: `tests/test_stepb_qa_invariants.py`.

---

## TABLE OF CONTENTS

1. [EXECUTIVE SUMMARY](#executive-summary)
2. [PROJECT ARCHITECTURE](#project-architecture)
3. [MODULE REGISTRY](#module-registry)
4. [NAVIGATION HIERARCHY](#navigation-hierarchy)
5. [DATABASE SCHEMA MAP](#database-schema-map)
6. [BUSINESS WORKFLOWS](#business-workflows)
7. [TRANSACTION CHAINS](#transaction-chains)
8. [DATA INTEGRITY RULES](#data-integrity-rules)
9. [EXISTING TEST COVERAGE](#existing-test-coverage)
10. [MAJOR RISKS FOUND](#major-risks-found)
11. [PROPOSED IMPLEMENTATION PLAN](#proposed-implementation-plan)

---

## EXECUTIVE SUMMARY

### Application Overview
AMS is a comprehensive Flask-based ERP system for FAZAL BUILDING MATERIALS, managing:
- **Clients & Suppliers** (Party Management)
- **Materials & Inventory** (Catalog & Stock)
- **Sales & Bookings** (Order Management)
- **GRN (Goods Received Notes)** (Procurement)
- **Payments & Receivables** (Financial Transactions)
- **Accounts & Ledgers** (Accounting)
- **Cash Flow Tracking** (Financial Management)
- **Delivery & Rentals** (Logistics)
- **Reports & Analytics** (Business Intelligence)

### Technology Stack
- **Backend:** Flask (Python 3.x)
- **ORM:** SQLAlchemy
- **Database:** SQLite (with WAL/DELETE journal mode detection)
- **Frontend:** Jinja2 Templates, Bootstrap 5.3.0, Flatpickr, Custom JavaScript
- **Authentication:** Flask-Login
- **Deployment:** PythonAnywhere, GitHub Actions

### Current State Assessment
Based on existing audit reports (AUDIT_REPORT.md, QA_FULL_AUDIT.md, SCHEMA_FAILURE_REPORT.md):

**CRITICAL ISSUES:**
1. Database currently EMPTY (0 bytes) - needs restoration
2. Hardcoded webhook token (`PakistanZindabad1947-2026`)
3. Hardcoded GitHub repo pointing to wrong project
4. Destructive auto-deploy with `git reset --hard`
5. Plaintext password fallback in User model
6. CSRF protection limited to `accounts.*` endpoints only
7. Concurrent sales allocate same auto bill numbers (PRED-001)
8. Future-dated receipts break reconciliation (PRED-002)
9. Open-Khata receivables invisible in reports (PRED-003, PRED-004)
10. FK constraint violations in wipe operations (PRED-009)

**POSITIVE ASPECTS:**
- Comprehensive module structure (157 blueprint files)
- Extensive template system (113 HTML templates)
- Strong model organization (13 model files)
- Existing test infrastructure (16 test files)
- Transaction atomicity generally working
- Foreign key enforcement enabled
- Minor-unit harmonization via SQLAlchemy events

---

## PROJECT ARCHITECTURE

### Directory Structure
```
rep-new-0001/
├── app/                          # Main application package
│   ├── __init__.py               # Application factory
│   ├── hooks.py                  # Global request hooks
│   ├── services/                 # Business logic services (12 files)
│   │   ├── __init__.py
│   │   ├── accounting.py         # Account transaction logic
│   │   ├── billing.py            # Bill number generation
│   │   ├── cash_flow_svc.py      # Cash flow management
│   │   ├── constants.py          # Application constants
│   │   ├── financial_ledgers.py  # Financial ledger building
│   │   ├── health.py             # Health checks
│   │   ├── instance_files.py     # Instance file management
│   │   ├── import_jobs.py         # Import job processing
│   │   ├── maintenance.py         # Maintenance tasks
│   │   ├── payments_crud.py      # Payment CRUD operations
│   │   ├── permissions.py        # User permissions
│   │   ├── schema.py             # Database schema management
│   │   └── v44_schema.py         # v4.4 schema initialization
│   └── blueprints/               # Route blueprints (105 files in app/blueprints)
│       ├── __init__.py
│       ├── api.py                # REST API endpoints
│       ├── auth.py               # Authentication routes
│       ├── core.py               # Core application routes
│       ├── ledgers/              # Ledger-related routes (18 files)
│       ├── masters/              # Master data routes (24 files)
│       ├── misc/                 # Miscellaneous routes (10 files)
│       ├── ops/                  # Operations routes (8 files)
│       ├── reports/              # Report routes (8 files)
│       ├── sales/                # Sales routes (13 files)
│       ├── system/               # System routes
│       └── migration.py          # Database migration routes
├── blueprints/                   # External blueprint modules (5 files)
│   ├── __init__.py
│   ├── accounts/                 # Accounts module
│   ├── admin.py
│   ├── data_lab.py
│   ├── import_export/            # Import/Export module
│   ├── inventory/                # Inventory module
│   └── module_template.py        # Template for new modules
├── models/                      # Database models (13 files)
│   ├── __init__.py
│   ├── __base__.py               # Base model utilities
│   ├── cash.py                  # Cash flow models
│   ├── catalog.py               # Material catalog models
│   ├── core.py                  # Core models (User, Settings, etc.)
│   ├── delivery.py              # Delivery models
│   ├── events.py                # Event models
│   ├── helpers.py               # Model helpers
│   ├── imports.py               # Import models
│   ├── migration.py             # Migration models
│   ├── ops_meta.py              # Operations metadata
│   ├── parties.py               # Party models (Client, Supplier)
│   ├── rentals.py               # Rental models
│   ├── sales.py                 # Sales models
│   └── stock.py                 # Stock models
├── templates/                   # Jinja2 templates (113 files)
│   ├── _ui/                     # UI components
│   ├── accounts/                # Accounts templates
│   ├── layout.html              # Main layout
│   ├── index.html               # Dashboard
│   └── ... (108 more templates)
├── static/                      # Static assets
│   ├── vendor/                  # Third-party libraries
│   │   ├── bootstrap/           # Bootstrap 5.3.0
│   │   ├── bootstrap-icons/     # Bootstrap Icons
│   │   └── flatpickr/            # Date picker
│   └── ... (CSS, JS, images)
├── tests/                       # Test suite (16 files)
│   ├── __init__.py
│   ├── frontend/                # Frontend tests
│   └── ... (15 more test files)
├── tools/                       # Utility tools (10 directories)
│   ├── deprecated/
│   ├── health/
│   ├── inventory/
│   ├── migrate/
│   ├── post_migration_audit/
│   ├── read_only/
│   ├── repair_controlled/
│   └── tests_isolated/
├── instance/                    # Runtime data
│   ├── ahmed_cement_v44_fresh.db # Current database (0 bytes - EMPTY)
│   ├── logs/                   # Application logs
│   ├── migration/              # Migration artifacts
│   └── .tmp/                   # Temporary files
├── docs/                        # Documentation
│   ├── daily-reconciliation-save-debug.md
│   └── legacy_migration_mapping.md
├── config.py                   # Deployment configuration
├── main.py                     # Entry point (development)
├── wsgi.py                     # Entry point (production)
└── requirements.txt            # Dependencies
```

### Entry Points
1. **Development:** `main.py` - Flask development server
2. **Production:** `wsgi.py` - WSGI application for PythonAnywhere
3. **Factory:** `app/__init__.py` - `create_app()` function

### Configuration System
- **Central Config:** `config.py` - Deployment control center
- **Environment Variables:** Extensive use of `os.environ` for runtime configuration
- **Instance Config:** `instance/` directory for runtime data and secrets

---

## MODULE REGISTRY

### Core Application Modules (app/blueprints/)

#### 1. AUTHENTICATION & AUTHORIZATION
- **Blueprint:** `auth.py`
- **Routes:** `/login`, `/logout`, `/recover`, `/reset_password`
- **Purpose:** User authentication, session management, password recovery
- **Models:** `User`, `UserLoginSession`, `RootRecoveryCode`
- **Services:** `permissions.py` (user loading, role checking)

#### 2. CORE ROUTES
- **Blueprint:** `core.py`
- **Routes:** `/`, `/dashboard`, `/settings`, `/backup`, `/health`
- **Purpose:** Main application entry points, dashboard, system settings
- **Templates:** `index.html`, `layout.html`

#### 3. MASTERS (Master Data Management)
- **Blueprint:** `masters/` (24 files)
- **Submodules:**
  - Clients: `clients.py`, `add_client.py`, `edit_client.py`, `delete_client.py`, `client_opening_balance.py`, `activate_all_clients.py`, `transfer_client.py`, `reclaim_client.py`, `toggle_delivery_person.py`
  - Suppliers: `suppliers.py`, `add_supplier.py`, `edit_supplier.py`, `delete_supplier.py`, `supplier_opening_balance.py`
  - Materials: `materials.py`, `add_material.py` (in blueprints/misc/)
  - Delivery Persons: `delivery_persons_page.py`, `add_delivery_person.py`, `edit_delivery_person.py`
  - Common: `_common.py`
- **Purpose:** CRUD operations for master data entities
- **Models:** `Client`, `Supplier`, `Material`, `MaterialCategory`, `DeliveryPerson`

#### 4. LEDGERS (Financial Ledgers)
- **Blueprint:** `ledgers/` (18 files)
- **Submodules:**
  - Client Ledger: `_client_client_ledger.py`, `_client_ledger_page.py`, `_client_download_client_ledger.py`, `_client_download_client_clearance.py`, `_client_financial_ledger.py`, `_client_financial_ledger_details.py`
  - Delivery Person: `delivery_person.py`
  - Booking: `booking_cancel.py`
  - Other: `other.py`
  - Common: `_common.py`
- **Purpose:** Financial ledger generation, client statements, transaction history
- **Models:** `Client`, `DirectSale`, `Payment`, `AccountTransaction`, `Entry`

#### 5. SALES (Sales Management)
- **Blueprint:** `sales/` (13 files)
- **Submodules:**
  - Bills: `_bills_delete_bill.py`, `_bills_download_invoice.py`, `_bills_edit_ledger_transaction.py`, `_bills_mixed_transactions.py`, `_bills_pending_bills.py`, `_bills_unvoid_transaction.py`, `_bills_view_bill.py`, `_bills_view_bill_detail.py`, `_bills_void_transaction.py`
  - Direct Sales: `_direct_sales_add_direct_sale.py`
  - Common: `_common.py`
- **Purpose:** Sales order management, billing, invoicing
- **Models:** `DirectSale`, `DirectSaleItem`, `Booking`, `BookingItem`, `PendingBill`, `Invoice`

#### 6. OPS (Operations)
- **Blueprint:** `ops/` (8 files)
- **Submodules:**
  - GRN: `grn.py`
  - Dispatch: `dispatch.py`, `_dispatch_add_record.py`, `_dispatch_delete_entry.py`, `_dispatch_dispatching.py`, `_dispatch_edit_entry.py`, `_dispatch_import_dispatch_data.py`, `_dispatch_tracking.py`
  - Delivery: `delivery.py`
  - Common: `_common.py`
- **Purpose:** Goods receipt, dispatch, delivery operations
- **Models:** `GRN`, `GRNItem`, `Dispatch`, `DispatchItem`, `Delivery`

#### 7. REPORTS (Reporting)
- **Blueprint:** `reports/` (8 files)
- **Submodules:**
  - Cash: `cash.py`
  - Profit: `profit.py`, `_profit_daily_transactions_redirect.py`, `_profit_financial_details.py`, `_profit_profit_reports.py`, `_profit_stock_summary_redirect.py`, `_profit_unpaid_transactions_page.py`
  - Common: `_common.py`
- **Purpose:** Business intelligence, financial reports, analytics
- **Models:** Various (read-only queries)

#### 8. MISC (Miscellaneous)
- **Blueprint:** `misc/` (10 files)
- **Submodules:**
  - Wipe: `_wipe_delete_selected_data.py`, `_wipe_admin_accounts_domain_wipe.py`, `_wipe_admin_rebuild_erp_consistency.py`, `_wipe_data_wipe_preview.py`, `_wipe_reconcile_data.py`, `_wipe_system_report.py`
  - Materials: `materials.py`
  - Users: `users_settings.py`
  - Pending: `pending.py`
  - Extra: `extra.py`
  - Common: `_common.py`
- **Purpose:** Data management, wipe operations, utilities
- **Models:** Various

#### 9. API (REST API)
- **Blueprint:** `api.py`
- **Routes:** `/api/client_booking_status/<client_code>`, `/api/client_financial_summary/<client_code>`, `/api/check_bill/<bill_no>`, `/api/notifications/due`
- **Purpose:** RESTful API endpoints for external integration
- **Services:** `app/services/api.py`

#### 10. SYSTEM (System Operations)
- **Blueprint:** `system/`
- **Purpose:** System-level operations, maintenance

#### 11. MIGRATION (Database Migration)
- **Blueprint:** `migration.py`
- **Purpose:** Database schema migration, version management

### External Blueprint Modules (blueprints/)

#### 12. ACCOUNTS
- **Location:** `blueprints/accounts/`
- **Purpose:** Accounting module (separate from app/blueprints/ledgers)
- **Status:** Appears to be a standalone module

#### 13. IMPORT_EXPORT
- **Location:** `blueprints/import_export/`
- **Purpose:** Data import/export functionality
- **Services:** `app/services/legacy_migration.py`, `blueprints/import_export/engine.py`

#### 14. INVENTORY
- **Location:** `blueprints/inventory/`
- **Purpose:** Inventory management module

#### 15. ADMIN
- **Location:** `blueprints/admin.py`
- **Purpose:** Admin dashboard and management

#### 16. DATA_LAB
- **Location:** `blueprints/data_lab.py`
- **Purpose:** Data manipulation and testing tools

---

## NAVIGATION HIERARCHY

### Sidebar Structure (Based on Template Analysis)

#### MAIN NAVIGATION SECTIONS

1. **DASHBOARD**
   - Route: `/`
   - Template: `index.html`
   - Purpose: Overview, KPIs, quick access

2. **SALES**
   - Direct Sales: `/direct_sales`, `/add_direct_sale`
   - Bookings: `/bookings`, `/add_booking`
   - Pending Bills: `/pending_bills`
   - Sales Returns: `/material_returns`
   - Invoices: `/invoices`

3. **PURCHASES**
   - GRN (Goods Received Notes): `/grn`, `/add_grn`
   - Suppliers: `/suppliers`, `/add_supplier`
   - Supplier Payments: `/supplier_payments`

4. **CLIENTS**
   - Client List: `/clients`
   - Add Client: `/add_client`
   - Client Ledger: `/client_ledger/<client_code>`
   - Outstanding Balance: `/client_outstanding/<client_code>`

5. **ACCOUNTS**
   - Account List: `/accounts`
   - Add Account: `/add_account`
   - Account Ledger: `/account_ledger/<account_id>`
   - Cash Flow: `/cash_flow`
   - Reconciliation: `/accounts/<id>/reconcile`
   - Transfers: `/add_transfer`

6. **INVENTORY**
   - Materials: `/materials`
   - Stock Summary: `/stock_summary`
   - Stock Movement: `/stock_movement`

7. **DELIVERY**
   - Delivery Persons: `/delivery_persons`
   - Dispatch: `/dispatch`
   - Delivery Tracking: `/delivery_tracking`

8. **REPORTS**
   - Daily Transactions: `/daily_transactions`
   - Financial Reports: `/financial_reports`
   - Stock Reports: `/stock_reports`
   - Client Reports: `/client_reports`
   - Supplier Reports: `/supplier_reports`
   - Profit & Loss: `/profit_loss`
   - Cash Flow Reports: `/cash_flow_reports`

9. **SETTINGS**
   - System Settings: `/settings`
   - User Management: `/users`
   - Backup & Restore: `/backup`
   - Import & Export: `/import_export`

10. **ADMIN**
    - Data Wipe: `/wipe`
    - System Health: `/health`
    - Migration: `/migration`

### Navigation Coverage Map

| Module | Sidebar Item | Page | Subpages | Status |
|--------|--------------|------|----------|--------|
| Dashboard | Dashboard | `/` | - | NOT ANALYZED |
| Sales | Direct Sales | `/direct_sales` | Add, Edit, View, Delete | NOT ANALYZED |
| Sales | Bookings | `/bookings` | Add, Edit, View, Cancel | NOT ANALYZED |
| Sales | Pending Bills | `/pending_bills` | View, Filter, Export | NOT ANALYZED |
| Purchases | GRN | `/grn` | Add, Edit, View, Delete | NOT ANALYZED |
| Purchases | Suppliers | `/suppliers` | Add, Edit, View, Delete | NOT ANALYZED |
| Clients | Clients | `/clients` | Add, Edit, View, Ledger | NOT ANALYZED |
| Accounts | Accounts | `/accounts` | Add, Edit, Ledger, Reconcile | NOT ANALYZED |
| Accounts | Cash Flow | `/cash_flow` | Categories, Subcategories | NOT ANALYZED |
| Inventory | Materials | `/materials` | Add, Edit, View | NOT ANALYZED |
| Delivery | Delivery Persons | `/delivery_persons` | Add, Edit, Toggle | NOT ANALYZED |
| Delivery | Dispatch | `/dispatch` | Add, Edit, Delete, Import | NOT ANALYZED |
| Reports | Daily Transactions | `/daily_transactions` | - | NOT ANALYZED |
| Reports | Financial Reports | `/financial_reports` | Profit, Stock Summary | NOT ANALYZED |
| Settings | Settings | `/settings` | - | NOT ANALYZED |
| Admin | Import/Export | `/import_export` | Full, Module, Legacy | NOT ANALYZED |
| Admin | Data Wipe | `/wipe` | Selective, Full | NOT ANALYZED |

**Status Legend:**
- NOT ANALYZED: Module identified but not yet deeply inspected
- ANALYZED: Module structure understood
- TESTED: Module has been functionally tested
- PASSED: Module tests passed
- FAILED: Module tests failed
- FIXED: Issues resolved
- RETESTED: Fixed modules retested

---

## DATABASE SCHEMA MAP

### Database Configuration
- **Type:** SQLite
- **File:** `instance/ahmed_cement_v44_fresh.db`
- **Current Size:** 0 bytes (EMPTY - needs restoration)
- **Schema Version:** v44 (AMS_SCHEMA_VERSION environment variable)
- **Journal Mode:** Auto-detected (WAL for local, DELETE for PythonAnywhere/NFS)
- **Foreign Keys:** Enabled via PRAGMA foreign_keys=ON on every connection
- **ORM:** SQLAlchemy with Flask-SQLAlchemy

### Model Files & Tables

#### 1. CORE MODELS (`models/core.py`)
- **User** - User accounts and authentication
  - Fields: id, username, password_hash, password_plain (CRITICAL: plaintext storage), role, status, permissions (30+ boolean flags), access_mode, created_at
  - Relationships: UserLoginSession (1:N), AuditLog (1:N)
  - Indexes: username (unique), role, status
  
- **UserLoginSession** - Browser login sessions
  - Fields: id, sid (unique), user_id, username, role, ip, user_agent, created_at, last_seen_at, ended_at
  - Indexes: sid, user_id, username, ip, created_at, last_seen_at, ended_at

- **Settings** - Application settings
  - Fields: id, currency, company_name, company_address, company_phone, company_email, tax_rate, invoice_prefix, bill_prefix, ui_theme, allow_global_negative_stock, google_* (OAuth), etc.

- **SchemaVersion** - Database schema version tracking
  - Fields: id, version, applied_at

- **RootRecoveryCode** - Root account recovery codes
  - Fields: id, username, code_hash, created_at, used_at, generated_by, note
  - Indexes: username, created_at, used_at

- **AuditLog** - General audit logging
  - Fields: id (UUID), user_id, username, action, details, timestamp
  - Indexes: username, timestamp

- **AccountingAuditLog** - Structured financial audit logging
  - Fields: id (UUID), module, action, entity_type, entity_id, user_id, username, ip_address, session_id, before_json, after_json, amount_before_minor, amount_after_minor, account_before_id, account_after_id, party_before_id, party_after_id, reason, created_at
  - Indexes: module, action, entity_type, entity_id, user_id, username, created_at

- **FutureAccountAuditLog** - Placeholder for future audit extensions
  - Fields: id, message, created_at

- **SystemLock** - System-wide mutex for critical operations
  - Fields: id, name (unique), status, owner, acquired_at, ttl_seconds, note
  - Indexes: name, status, acquired_at

#### 2. PARTIES MODELS (`models/parties.py`)
- **Client** - Customer master data
  - Fields: id, code (unique), name, phone, address, city, opening_balance, opening_balance_date, credit_limit, is_active, created_at, updated_at
  - Relationships: DirectSale (1:N), Payment (1:N), Booking (1:N), ClientLedgerEntry (1:N)
  - Indexes: code, name, phone, is_active, created_at

- **Supplier** - Vendor master data
  - Fields: id, code (unique), name, phone, address, city, opening_balance, opening_balance_date, is_active, created_at, updated_at
  - Relationships: GRN (1:N), SupplierPayment (1:N)
  - Indexes: code, name, phone, is_active, created_at

- **DeliveryPerson** - Delivery personnel
  - Fields: id, name (unique), phone, opening_balance, opening_balance_date, is_active, created_at
  - Relationships: DirectSale (via sale_delivery_persons), Dispatch (1:N)
  - Indexes: name, is_active

#### 3. CATALOG MODELS (`models/catalog.py`)
- **MaterialCategory** - Material classification
  - Fields: id, name (unique), description, is_active, created_at
  - Relationships: Material (1:N)

- **Material** - Product/inventory items
  - Fields: id, code (unique), name, category_id (FK), unit, purchase_rate, sale_rate, opening_stock, opening_stock_date, current_stock, reorder_level, is_active, created_at, updated_at
  - Relationships: MaterialCategory (N:1), GRNItem (1:N), DirectSaleItem (1:N), BookingItem (1:N), MaterialReturnItem (1:N), StockMovement (1:N)
  - Indexes: code, name, category_id, is_active

#### 4. STOCK MODELS (`models/stock.py`)
- **StockMovement** - Inventory movement tracking
  - Fields: id, material_id (FK), movement_type, quantity, reference_type, reference_id, rate, amount, balance_after, notes, created_at
  - Indexes: material_id, reference_type, reference_id, created_at

- **GRN** - Goods Received Note
  - Fields: id, supplier_id (FK), manual_bill_no (unique), auto_bill_no (unique), date, total_amount, total_quantity, freight_cost, notes, is_void, voided_at, voided_by, void_reason, created_at, updated_at
  - Relationships: Supplier (N:1), GRNItem (1:N), GRNAllocation (1:N)
  - Indexes: supplier_id, manual_bill_no, auto_bill_no, date, is_void

- **GRNItem** - Individual items in a GRN
  - Fields: id, grn_id (FK), material_id (FK), quantity, received_quantity, rate, amount, batch_no, expiry_date, is_locked, notes, created_at
  - Relationships: GRN (N:1), Material (N:1), DirectSaleItem (1:N via grn_item_id)
  - Indexes: grn_id, material_id, is_locked

- **GRNAllocation** - GRN lot allocation tracking
  - Fields: id, grn_item_id (FK), allocated_quantity, remaining_quantity, notes, created_at
  - Relationships: GRNItem (N:1)

#### 5. SALES MODELS (`models/sales.py`)
- **DirectSale** - Direct sales transactions
  - Fields: id, client_id (FK), client_code, manual_bill_no (unique), auto_bill_no (unique), date, total_quantity, total_amount, total_discount, net_amount, payment_status, payment_amount, balance_amount, delivery_person_id (FK), notes, is_void, voided_at, voided_by, void_reason, idempotency_key, idempotency_payload_hash, created_at, updated_at
  - Relationships: Client (N:1), DeliveryPerson (N:1), DirectSaleItem (1:N), Entry (1:N), Payment (1:N via direct_sale_id), Invoice (1:1)
  - Indexes: client_id, client_code, manual_bill_no, auto_bill_no, date, is_void, delivery_person_id, idempotency_key

- **DirectSaleItem** - Individual items in a direct sale
  - Fields: id, direct_sale_id (FK), material_id (FK), grn_item_id (FK), quantity, rate, amount, discount, net_amount, notes, created_at
  - Relationships: DirectSale (N:1), Material (N:1), GRNItem (N:1)
  - Indexes: direct_sale_id, material_id, grn_item_id

- **Booking** - Sales bookings/reservations
  - Fields: id, client_id (FK), client_code, manual_bill_no (unique), auto_bill_no (unique), date, total_quantity, total_amount, status, delivery_person_id (FK), notes, is_void, voided_at, voided_by, void_reason, created_at, updated_at
  - Relationships: Client (N:1), DeliveryPerson (N:1), BookingItem (1:N), Entry (1:N)
  - Indexes: client_id, client_code, manual_bill_no, auto_bill_no, date, status, is_void, delivery_person_id

- **BookingItem** - Individual items in a booking
  - Fields: id, booking_id (FK), material_id (FK), quantity, rate, amount, notes, created_at
  - Relationships: Booking (N:1), Material (N:1)
  - Indexes: booking_id, material_id

- **PendingBill** - Outstanding receivables
  - Fields: id, client_id (FK), client_code, direct_sale_id (FK), booking_id (FK), entry_id (FK), amount, date, due_date, status, notes, created_at, updated_at
  - Relationships: Client (N:1), DirectSale (N:1), Booking (N:1), Entry (N:1)
  - Indexes: client_id, client_code, direct_sale_id, booking_id, entry_id, date, due_date, status

- **Invoice** - Customer invoices
  - Fields: id, invoice_no (unique), client_id (FK), direct_sale_id (FK), booking_id (FK), entry_id (FK), amount, date, status, notes, created_at
  - Relationships: Client (N:1), DirectSale (N:1), Booking (N:1), Entry (N:1)
  - Indexes: invoice_no, client_id, direct_sale_id, booking_id, entry_id, date, status

- **MaterialReturn** - Material return transactions
  - Fields: id, client_id (FK), client_code, manual_bill_no (unique), auto_bill_no (unique), date, total_quantity, total_amount, notes, is_void, voided_at, voided_by, void_reason, created_at, updated_at
  - Relationships: Client (N:1), MaterialReturnItem (1:N), Entry (1:N)
  - Indexes: client_id, client_code, manual_bill_no, auto_bill_no, date, is_void

- **MaterialReturnItem** - Individual items in a material return
  - Fields: id, material_return_id (FK), material_id (FK), quantity, rate, amount, notes, created_at
  - Relationships: MaterialReturn (N:1), Material (N:1)
  - Indexes: material_return_id, material_id

#### 6. CASH MODELS (`models/cash.py`)
- **Payment** - Customer payments
  - Fields: id, client_id (FK), client_code, payment_no (unique), date, amount, payment_method, account_id (FK), reference_no, notes, is_void, voided_at, voided_by, void_reason, direct_sale_id (FK), booking_id (FK), entry_id (FK), idempotency_key, revision, created_at, updated_at
  - Relationships: Client (N:1), Account (N:1), DirectSale (N:1), Booking (N:1), Entry (N:1), PaymentAccountTransaction (1:N)
  - Indexes: client_id, client_code, payment_no, date, account_id, is_void, direct_sale_id, booking_id, entry_id, idempotency_key

- **SupplierPayment** - Supplier payments
  - Fields: id, supplier_id (FK), payment_no (unique), date, amount, payment_method, account_id (FK), reference_no, notes, is_void, voided_at, voided_by, void_reason, grn_id (FK), idempotency_key, revision, created_at, updated_at
  - Relationships: Supplier (N:1), Account (N:1), GRN (N:1)
  - Indexes: supplier_id, payment_no, date, account_id, is_void, grn_id, idempotency_key

- **Account** - Financial accounts (Cash, Bank, etc.)
  - Fields: id, code (unique), name, classification, class_category, channel, linked_party_type, linked_party_id, linked_party_name, opening_balance, opening_balance_date, opening_position, balance, balance_minor, is_active, notes, created_at, updated_at
  - Relationships: AccountTransaction (1:N), Payment (1:N), SupplierPayment (1:N)
  - Indexes: code, name, classification, is_active, linked_party_type, linked_party_id

- **AccountTransaction** - Financial transactions
  - Fields: id, account_id (FK), transaction_type, amount, amount_minor, balance_after, balance_after_minor, reference_type, reference_id, reference_no, date, description, is_void, voided_at, voided_by, void_reason, from_account_id (FK), to_account_id (FK), created_at, updated_at
  - Relationships: Account (N:1), from_account (N:1), to_account (N:1)
  - Indexes: account_id, transaction_type, date, reference_type, reference_id, is_void, from_account_id, to_account_id

- **AccountReconciliation** - Account reconciliation records
  - Fields: id, account_id (FK), reconciliation_date, period_start, period_end, opening_balance, opening_balance_minor, expected_balance, expected_balance_minor, actual_balance, actual_balance_minor, difference, difference_minor, final_reconciled_balance, final_reconciled_balance_minor, notes, created_at, updated_at
  - Relationships: Account (N:1)
  - Indexes: account_id, reconciliation_date, period_start, period_end

- **CashFlowEntry** - Cash flow transactions
  - Fields: id, date, amount, amount_minor, category, subcategory, party_type, party_id, party_name, reference_type, reference_id, reference_no, description, flow_type, created_at, updated_at
  - Indexes: date, category, subcategory, party_type, party_id, reference_type, reference_id, flow_type

- **CashFlowCategory** - Cash flow categorization
  - Fields: id, name (unique), description, is_active, created_at
  - Relationships: CashFlowSubcategory (1:N)
  - Indexes: name, is_active

- **CashFlowSubcategory** - Cash flow sub-categorization
  - Fields: id, category_id (FK), name (unique), description, is_active, created_at
  - Relationships: CashFlowCategory (N:1)
  - Indexes: category_id, name, is_active

- **CashFlowDifferenceAdjustment** - Cash flow adjustment entries
  - Fields: id, date, amount, amount_minor, reason, notes, created_at
  - Indexes: date

- **BillCounter** - Bill number sequencing
  - Fields: id, namespace (unique), count, last_updated
  - Indexes: namespace

#### 7. DELIVERY MODELS (`models/delivery.py`)
- **Delivery** - Delivery records
  - Fields: id, direct_sale_id (FK), booking_id (FK), delivery_person_id (FK), date, status, notes, created_at, updated_at
  - Relationships: DirectSale (N:1), Booking (N:1), DeliveryPerson (N:1)
  - Indexes: direct_sale_id, booking_id, delivery_person_id, date, status

- **DeliveryRent** - Delivery rent/settlement
  - Fields: id, delivery_person_id (FK), date, amount, status, notes, created_at, updated_at
  - Relationships: DeliveryPerson (N:1), DeliveryPersonPayment (1:N)
  - Indexes: delivery_person_id, date, status

- **DeliveryPersonPayment** - Payments to delivery personnel
  - Fields: id, delivery_rent_id (FK), amount, payment_method, account_id (FK), date, notes, created_at
  - Relationships: DeliveryRent (N:1), Account (N:1)
  - Indexes: delivery_rent_id, account_id, date

#### 8. RENTALS MODELS (`models/rentals.py`)
- **FbmRental** - Rental transactions
  - Fields: id, client_id (FK), item_name, quantity, rate, amount, date, return_date, status, notes, created_at, updated_at
  - Relationships: Client (N:1), FbmRentalItem (1:N)
  - Indexes: client_id, date, status

- **FbmRentalItem** - Individual rental items
  - Fields: id, rental_id (FK), item_code, item_name, quantity, rate, amount, returned_quantity, condition, notes
  - Relationships: FbmRental (N:1)
  - Indexes: rental_id, item_code

- **FbmRentalClient** - Rental-specific client data
  - Fields: id, client_id (FK, unique), rental_balance, notes, created_at, updated_at
  - Relationships: Client (1:1)
  - Indexes: client_id

- **FbmCashDrawerEntry** - Cash drawer tracking
  - Fields: id, date, amount, amount_minor, entry_type, reference_type, reference_id, notes, created_at
  - Indexes: date, entry_type, reference_type, reference_id

#### 9. IMPORTS MODELS (`models/imports.py`)
- **ImportJob** - Import job tracking
  - Fields: id, job_type, status, source_file, total_rows, processed_rows, failed_rows, error_message, started_at, completed_at, created_by, created_at
  - Indexes: job_type, status, started_at, completed_at

- **ImportJobItem** - Individual import items
  - Fields: id, import_job_id (FK), sequence, table_name, action, key_value, data_json, status, error_message, created_at
  - Relationships: ImportJob (N:1)
  - Indexes: import_job_id, sequence, table_name, status

#### 10. MIGRATION MODELS (`models/migration.py`)
- **MigrationAudit** - Migration audit logging
  - Fields: id, migration_name, status, started_at, completed_at, rows_affected, error_message, created_at
  - Indexes: migration_name, status, started_at

- **DataPurgeLog** - Data purge logging
  - Fields: id, purge_type, tables_affected, rows_removed, rows_kept, backup_path, performed_by, performed_at, notes
  - Indexes: purge_type, performed_at

#### 11. EVENTS MODELS (`models/events.py`)
- **StockEvent** - Stock-related events
  - Fields: id, event_type, material_id, quantity_before, quantity_after, reference_type, reference_id, notes, created_at
  - Indexes: event_type, material_id, reference_type, reference_id, created_at

- **FinancialEvent** - Financial events
  - Fields: id, event_type, account_id, amount_before, amount_after, reference_type, reference_id, notes, created_at
  - Indexes: event_type, account_id, reference_type, reference_id, created_at

- **AuditEvent** - General audit events
  - Fields: id, event_type, entity_type, entity_id, user_id, old_value, new_value, notes, created_at
  - Indexes: event_type, entity_type, entity_id, user_id, created_at

#### 12. OPS_META MODELS (`models/ops_meta.py`)
- **OperationMetadata** - Operation metadata tracking
  - Fields: id, operation_type, reference_type, reference_id, status, started_at, completed_at, duration_seconds, notes, created_at
  - Indexes: operation_type, reference_type, reference_id, status, started_at

- **BatchOperation** - Batch operation tracking
  - Fields: id, batch_type, total_items, processed_items, failed_items, status, started_at, completed_at, created_by, notes
  - Indexes: batch_type, status, started_at

### Foreign Key Relationships Summary

```
USER -> UserLoginSession (1:N)
USER -> AuditLog (1:N)
USER -> AccountingAuditLog (1:N)

CLIENT -> DirectSale (1:N)
CLIENT -> Payment (1:N)
CLIENT -> Booking (1:N)
CLIENT -> MaterialReturn (1:N)
CLIENT -> PendingBill (1:N)
CLIENT -> Invoice (1:N)

SUPPLIER -> GRN (1:N)
SUPPLIER -> SupplierPayment (1:N)

MATERIAL_CATEGORY -> Material (1:N)
MATERIAL -> GRNItem (1:N)
MATERIAL -> DirectSaleItem (1:N)
MATERIAL -> BookingItem (1:N)
MATERIAL -> MaterialReturnItem (1:N)
MATERIAL -> StockMovement (1:N)

GRN -> GRNItem (1:N)
GRN -> SupplierPayment (1:N via grn_id)
GRNItem -> GRNAllocation (1:N)
GRNItem -> DirectSaleItem (1:N via grn_item_id)

DIRECT_SALE -> DirectSaleItem (1:N)
DIRECT_SALE -> Payment (1:N via direct_sale_id)
DIRECT_SALE -> Entry (1:N)
DIRECT_SALE -> Invoice (1:1)
DIRECT_SALE -> PendingBill (1:N)
DIRECT_SALE -> Delivery (1:N)

BOOKING -> BookingItem (1:N)
BOOKING -> Entry (1:N)
BOOKING -> PendingBill (1:N)
BOOKING -> Delivery (1:N)

MATERIAL_RETURN -> MaterialReturnItem (1:N)
MATERIAL_RETURN -> Entry (1:N)

ACCOUNT -> AccountTransaction (1:N)
ACCOUNT -> Payment (1:N)
ACCOUNT -> SupplierPayment (1:N)
ACCOUNT -> DeliveryPersonPayment (1:N)
ACCOUNT -> AccountReconciliation (1:N)

ACCOUNT_TRANSACTION -> Account (N:1 from_account)
ACCOUNT_TRANSACTION -> Account (N:1 to_account)

PAYMENT -> DirectSale (N:1)
PAYMENT -> Booking (N:1)
PAYMENT -> Entry (N:1)
PAYMENT -> Account (N:1)

SUPPLIER_PAYMENT -> Supplier (N:1)
SUPPLIER_PAYMENT -> GRN (N:1)
SUPPLIER_PAYMENT -> Account (N:1)

DELIVERY_PERSON -> DirectSale (N:1 via sale_delivery_persons)
DELIVERY_PERSON -> Delivery (1:N)
DELIVERY_PERSON -> DeliveryRent (1:N)

DELIVERY_RENT -> DeliveryPersonPayment (1:N)

CASH_FLOW_CATEGORY -> CashFlowSubcategory (1:N)
CASH_FLOW_SUBCATEGORY -> CashFlowEntry (1:N via category/subcategory)

IMPORT_JOB -> ImportJobItem (1:N)
```

### Indexes Summary

**Unique Indexes:**
- User.username
- Client.code
- Supplier.code
- Material.code
- MaterialCategory.name
- DeliveryPerson.name
- DirectSale.manual_bill_no (partial: WHERE auto_bill_no IS NOT NULL)
- DirectSale.auto_bill_no (partial: WHERE auto_bill_no IS NOT NULL)
- Booking.manual_bill_no
- Booking.auto_bill_no
- GRN.manual_bill_no
- GRN.auto_bill_no
- MaterialReturn.manual_bill_no
- MaterialReturn.auto_bill_no
- Payment.payment_no
- SupplierPayment.payment_no
- Account.code
- Invoice.invoice_no
- CashFlowCategory.name
- CashFlowSubcategory.name (per category)
- BillCounter.namespace

**Regular Indexes:**
- All foreign key columns
- All date/time columns
- All status/enum columns
- All is_active flags
- All created_at/updated_at columns

---

## BUSINESS WORKFLOWS

### 1. CLIENT FLOW

```
CREATE CLIENT
├── Input: name, code, phone, address, opening_balance, credit_limit
├── Validation: code uniqueness, required fields
├── Database: Insert Client record
├── Side Effects: None
└── Related: Client list, Client ledger availability

VIEW CLIENT
├── Query: Client by ID/code
├── Display: Basic info, opening balance, credit limit, status
├── Related Links: Ledger, Outstanding, Bookings, Sales, Payments
└── Side Effects: None

EDIT CLIENT
├── Input: Updated client fields
├── Validation: Same as create
├── Database: Update Client record
├── Side Effects: None
└── Related: All client references remain valid

CLIENT LEDGER
├── Query: All transactions for client (sales, payments, returns, adjustments)
├── Calculation: Running balance from opening + sales - payments - returns
├── Display: Transaction list with dates, amounts, running balance
├── Export: Download as CSV/Excel
└── Side Effects: None

CLIENT OUTSTANDING
├── Query: Pending bills for client
├── Calculation: Sum of outstanding amounts
├── Display: Total outstanding, aging breakdown
└── Side Effects: None

DELETE CLIENT
├── Validation: Check for existing transactions (sales, payments, etc.)
├── Database: Delete Client record (or mark as inactive)
├── Side Effects: Cascade delete or nullify references
└── Restriction: Blocked if transactions exist (configurable)
```

**Financial Impact:** Client creation affects receivables reporting. Opening balance affects initial outstanding calculation.

**Inventory Impact:** None

**Ledger Impact:** Client ledger is derived from transactions, not affected by client master changes.

**Reports Affected:** Current Payables, Client Reports, Dashboard receivables

**Dashboard Metrics Affected:** Total Receivables, Client Count

**Known Risks:**
- Deleting client with transactions can orphan records
- Opening balance changes don't automatically update ledger

**Test Scenarios:**
1. Create client with valid data
2. Create client with duplicate code
3. Create client with missing required fields
4. Edit client and verify persistence
5. View client ledger with multiple transactions
6. Delete client with no transactions
7. Attempt to delete client with existing transactions

---

### 2. MATERIAL FLOW

```
CREATE MATERIAL
├── Input: name, code, category, unit, purchase_rate, sale_rate, opening_stock
├── Validation: code uniqueness, category exists, required fields
├── Database: Insert Material record
├── Side Effects: Update category material count
└── Related: Material list, Stock summary

VIEW MATERIAL
├── Query: Material by ID/code
├── Display: Basic info, current stock, rates, category
├── Related Links: Stock movements, GRN items, Sales items
└── Side Effects: None

EDIT MATERIAL
├── Input: Updated material fields
├── Validation: Same as create
├── Database: Update Material record
├── Side Effects: None
└── Related: All material references remain valid

STOCK MOVEMENT
├── Input: material_id, movement_type (IN/OUT), quantity, reference
├── Validation: Material exists, quantity > 0
├── Database: Insert StockMovement record
├── Side Effects: Update Material.current_stock
└── Related: Stock reports, Inventory valuation

DELETE MATERIAL
├── Validation: Check for existing transactions (GRN items, sales items, etc.)
├── Database: Delete Material record (or mark as inactive)
├── Side Effects: Cascade delete or nullify references
└── Restriction: Blocked if transactions exist
```

**Financial Impact:** Material rates affect sales pricing and COGS calculation.

**Inventory Impact:** Opening stock sets initial inventory level. Stock movements track all changes.

**Ledger Impact:** None directly (inventory is tracked separately from accounting)

**Reports Affected:** Stock Summary, Stock Movement Report, Inventory Valuation

**Dashboard Metrics Affected:** Current Stock Value, Material Count

**Known Risks:**
- Deleting material with stock can cause inventory discrepancies
- Rate changes don't affect historical transactions

**Test Scenarios:**
1. Create material with valid data
2. Create material with duplicate code
3. Create material with non-existent category
4. Receive GRN and verify stock increase
5. Make sale and verify stock decrease
6. View stock movement history
7. Delete material with no transactions
8. Attempt to delete material with existing stock

---

### 3. GRN (GOODS RECEIVED NOTE) FLOW

```
CREATE GRN
├── Input: supplier_id, date, items (material_id, quantity, rate), freight_cost
├── Validation: Supplier exists, materials exist, quantities > 0
├── Database: Insert GRN record + GRNItem records
├── Side Effects: 
│   ├── Increase Material.current_stock (if received)
│   ├── Create StockMovement records (IN type)
│   ├── Create Entry records (debit inventory, credit supplier)
│   └── Update Supplier opening balance if configured
└── Related: GRN list, Supplier ledger, Stock summary

VIEW GRN
├── Query: GRN by ID + GRNItem records
├── Display: Header info, supplier details, item list with quantities/rates
├── Related Links: Edit, Delete, Print, Related Sales
└── Side Effects: None

EDIT GRN
├── Input: Updated GRN fields and/or items
├── Validation: Same as create + check for locked lots
├── Database: 
│   ├── If items unchanged: Update GRN record
│   └── If items changed: Void old entries, create new GRN + entries
├── Side Effects: 
│   ├── Stock adjustment (if quantities changed)
│   ├── Entry reversal and recreation
│   └── GRNItem locking if used in sales
└── Restriction: Cannot edit if lots are locked (used in sales)

DELETE GRN
├── Validation: Check for locked lots (used in sales)
├── Database: 
│   ├── Void GRN record
│   ├── Void related Entry records
│   ├── Decrease Material.current_stock
│   └── Create StockMovement records (OUT type for reversal)
└── Restriction: Blocked if lots are locked
```

**Financial Impact:**
- GRN amount increases supplier payable
- Freight cost increases expenses
- Inventory value increases by GRN amount

**Inventory Impact:** Material stock increases by received quantity

**Ledger Impact:**
- Debit: Inventory account (or Material account)
- Credit: Supplier account (or Accounts Payable)

**Reports Affected:** GRN Register, Supplier Ledger, Stock Reports, Financial Reports

**Dashboard Metrics Affected:** Total Purchases, Current Stock, Supplier Payables

**Known Risks:**
- Concurrent GRN creation can cause bill number conflicts
- Editing GRN with locked lots is blocked (PRED-012)
- Delete order doesn't handle all FK dependencies (PRED-009)

**Test Scenarios:**
1. Create GRN with multiple items
2. Create GRN with non-existent supplier
3. View GRN with all details
4. Edit GRN before any sales use its lots
5. Attempt to edit GRN after sales use its lots
6. Delete GRN with no sales
7. Attempt to delete GRN with existing sales
8. Verify stock increases after GRN
9. Verify entries created for GRN

---

### 4. DIRECT SALE FLOW

```
CREATE DIRECT SALE
├── Input: client_id, date, items (material_id, quantity, rate, discount), delivery_person_id, payment_status
├── Validation: 
│   ├── Client exists
│   ├── Materials exist
│   ├── Quantities > 0
│   ├── Stock available (if not allowing negative stock)
│   └── Rates valid
├── Database: Insert DirectSale record + DirectSaleItem records
├── Side Effects: 
│   ├── Decrease Material.current_stock
│   ├── Create StockMovement records (OUT type)
│   ├── Create Entry records (credit sales, debit inventory)
│   ├── Create PendingBill record (if not fully paid)
│   ├── Update Client outstanding balance
│   └── Update DeliveryPerson balance if applicable
└── Related: Sales list, Client ledger, Stock summary, Pending bills

VIEW SALE
├── Query: DirectSale by ID + DirectSaleItem records + related Payment/Entry records
├── Display: Header info, client details, item list, totals, payment status
├── Related Links: Edit, Delete, Print Invoice, View Ledger, View Payments
└── Side Effects: None

EDIT SALE
├── Input: Updated sale fields and/or items
├── Validation: Same as create + check for voided status
├── Database: 
│   ├── If not voided: Void old entries, create new sale + entries
│   └── If voided: Reject edit
├── Side Effects: 
│   ├── Stock adjustment (if quantities changed)
│   ├── Entry reversal and recreation
│   └── PendingBill update
└── Restriction: Cannot edit voided sales

DELETE SALE
├── Validation: Check if sale is voided or has payments
├── Database: 
│   ├── Void DirectSale record
│   ├── Void related Entry records
│   ├── Increase Material.current_stock
│   ├── Create StockMovement records (IN type for reversal)
│   ├── Void/update related PendingBill
│   └── Update Client outstanding balance
└── Restriction: Blocked if partially paid (configurable)

VOID SALE
├── Same as delete but keeps record for audit
└── Creates reversal entries
```

**Financial Impact:**
- Sale amount increases client receivable
- COGS decreases inventory value
- Sales revenue increases
- Tax amount increases if applicable

**Inventory Impact:** Material stock decreases by sold quantity

**Ledger Impact:**
- Debit: Client account (Receivable)
- Credit: Sales account (Revenue)
- Debit: COGS account (Expense)
- Credit: Inventory account (Asset reduction)

**Reports Affected:** Sales Register, Client Ledger, Stock Reports, Financial Reports, Pending Bills

**Dashboard Metrics Affected:** Total Sales, Current Stock, Client Receivables

**Known Risks:**
- Concurrent sales can allocate same auto bill number (PRED-001)
- Un-keyed POST replay duplicates sales (PRED-006)
- Idempotency key reused with different payload loses data (PRED-007)

**Test Scenarios:**
1. Create sale with multiple items
2. Create sale with insufficient stock (should fail)
3. Create sale with partial payment
4. Create sale with full payment
5. View sale with all details
6. Edit sale before any payments
7. Attempt to edit voided sale
8. Delete sale with no payments
9. Attempt to delete sale with partial payment
10. Void sale and verify reversal
11. Concurrent sale creation (8 threads) - verify unique bill numbers
12. Duplicate POST submission - verify idempotency

---

### 5. PAYMENT FLOW

```
CREATE PAYMENT (Client Payment)
├── Input: client_id, date, amount, payment_method, account_id, reference_no, direct_sale_id (optional)
├── Validation: 
│   ├── Client exists
│   ├── Account exists
│   ├── Amount > 0
│   └── Check reconcile period is open (PRED-008)
├── Database: Insert Payment record + AccountTransaction records
├── Side Effects: 
│   ├── Decrease Client outstanding balance
│   ├── Increase Account balance
│   ├── Create AccountTransaction (debit cash/bank, credit receivable)
│   ├── Update related DirectSale/Booking payment status
│   └── Create AccountingAuditLog entry
└── Related: Payments list, Client ledger, Account ledger

CREATE PAYMENT (Supplier Payment)
├── Input: supplier_id, date, amount, payment_method, account_id, reference_no, grn_id (optional)
├── Validation: Same as client payment
├── Database: Insert SupplierPayment record + AccountTransaction records
├── Side Effects: 
│   ├── Decrease Supplier payable balance
│   ├── Increase Account balance
│   └── Create AccountTransaction (debit accounts payable, credit cash/bank)
└── Related: Supplier payments list, Supplier ledger, Account ledger

VIEW PAYMENT
├── Query: Payment/SupplierPayment by ID + related AccountTransaction records
├── Display: Header info, party details, amount, method, account, reference
├── Related Links: Edit, Void, Print Receipt
└── Side Effects: None

EDIT PAYMENT
├── Input: Updated payment fields
├── Validation: Check reconcile period is open (PRED-008)
├── Database: Update Payment record
├── Side Effects: 
│   ├── AccountTransaction reversal and recreation if amount changed
│   └── Client/Supplier balance update
└── Restriction: Cannot edit if reconcile period is closed

VOID PAYMENT
├── Database: 
│   ├── Void Payment record
│   ├── Void related AccountTransaction records
│   ├── Increase Client/Supplier outstanding balance
│   └── Decrease Account balance
└── Creates reversal entries

DELETE PAYMENT
├── Same as void but removes record (configurable)
└── Restriction: Usually blocked, void preferred for audit
```

**Financial Impact:**
- Client payment reduces receivables
- Supplier payment reduces payables
- Account balance changes based on payment method

**Inventory Impact:** None

**Ledger Impact:**
- Client Payment: Debit Cash/Bank, Credit Client Receivable
- Supplier Payment: Debit Supplier Payable, Credit Cash/Bank

**Reports Affected:** Payment Register, Client Ledger, Supplier Ledger, Account Ledger, Cash Flow

**Dashboard Metrics Affected:** Cash Balance, Total Receivables, Total Payables

**Known Risks:**
- Future-dated receipts break reconciliation (PRED-002)
- Reconcile period guard bypassed on payment create (PRED-008)

**Test Scenarios:**
1. Create client payment with valid data
2. Create client payment with non-existent client
3. Create client payment with amount exceeding outstanding
4. View payment details
5. Edit payment amount
6. Void payment and verify reversal
7. Create payment in reconciled period (should fail)
8. Create future-dated payment and verify reconciliation impact
9. Concurrent payment creation

---

### 6. BOOKING FLOW

```
CREATE BOOKING
├── Input: client_id, date, items (material_id, quantity, rate), delivery_person_id, status
├── Validation: 
│   ├── Client exists
│   ├── Materials exist
│   ├── Quantities > 0
│   └── Status valid (Pending, Confirmed, Delivered, Cancelled)
├── Database: Insert Booking record + BookingItem records
├── Side Effects: 
│   ├── Reserve Material stock (if configured)
│   ├── Create PendingBill record
│   └── Update Client booked balance
└── Related: Bookings list, Client bookings, Delivery schedule

VIEW BOOKING
├── Query: Booking by ID + BookingItem records
├── Display: Header info, client details, item list, status, delivery info
├── Related Links: Edit, Cancel, Convert to Sale, Deliver
└── Side Effects: None

EDIT BOOKING
├── Input: Updated booking fields and/or items
├── Validation: Same as create
├── Database: Update Booking record + BookingItem records
├── Side Effects: 
│   ├── Stock reservation adjustment
│   └── PendingBill update
└── Restriction: Cannot edit delivered/cancelled bookings

DELIVER BOOKING
├── Input: booking_id, delivered_items, delivery_person_id
├── Validation: Booking exists, items match, status allows delivery
├── Database: 
│   ├── Update Booking status to Delivered
│   ├── Create DirectSale record from booking
│   ├── Create DirectSaleItem records
│   ├── Decrease reserved stock, decrease available stock
│   └── Update PendingBill status
├── Side Effects: 
│   ├── Stock movement (OUT type)
│   ├── Entry creation
│   └── Client outstanding update
└── Creates sale from booking

CANCEL BOOKING
├── Database: 
│   ├── Void/Update Booking record
│   ├── Release reserved stock
│   ├── Void related PendingBill
│   └── Create AccountingAuditLog entry
└── Creates reversal entries
```

**Financial Impact:**
- Booking creates receivable when delivered
- Cancelled booking reverses any reservations

**Inventory Impact:** Booking reserves stock, delivery consumes it

**Ledger Impact:**
- On delivery: Same as direct sale
- On cancellation: Reversal entries

**Reports Affected:** Bookings Register, Client Ledger, Stock Reports, Pending Bills

**Dashboard Metrics Affected:** Booked Quantity, Pending Deliveries

**Known Risks:**
- Booking cancel logic may not fully clean children

**Test Scenarios:**
1. Create booking with multiple items
2. View booking details
3. Edit booking before delivery
4. Deliver booking and verify sale creation
5. Cancel booking and verify stock release
6. Attempt to edit delivered booking
7. Create booking with insufficient stock

---

### 7. ACCOUNT FLOW

```
CREATE ACCOUNT
├── Input: code, name, classification, class_category, channel, linked_party, opening_balance, opening_position
├── Validation: code uniqueness, valid classification
├── Database: Insert Account record
├── Side Effects: 
│   ├── Create opening balance AccountTransaction if opening_balance > 0
│   └── Update Account.balance
└── Related: Accounts list, Account ledger

VIEW ACCOUNT
├── Query: Account by ID + AccountTransaction records
├── Display: Header info, current balance, opening balance, classification, linked party
├── Related Links: Edit, Ledger, Reconcile, Transfers
└── Side Effects: None

EDIT ACCOUNT
├── Input: Updated account fields
├── Validation: 
│   ├── Code uniqueness (if changed)
│   ├── Opening balance changes affect current balance
│   └── Classification affects available fields
├── Database: Update Account record
├── Side Effects: 
│   ├── If opening_balance changed: Create adjustment AccountTransaction
│   └── Update Account.balance
└── Related: All account references remain valid

RECONCILE ACCOUNT
├── Input: account_id, reconciliation_date, period_start, period_end, actual_balance
├── Validation: 
│   ├── Account exists
│   ├── Period not already reconciled
│   └── Actual balance matches expected (calculated from transactions)
├── Database: 
│   ├── Insert AccountReconciliation record
│   ├── If difference exists: Create adjustment AccountTransaction
│   └── Update Account.balance to final_reconciled_balance
├── Side Effects: 
│   ├── Locks period for further changes (PRED-008)
│   └── Creates AccountingAuditLog entry
└── Related: Account reconciliation history

TRANSFER FUNDS
├── Input: from_account_id, to_account_id, date, amount, description
├── Validation: 
│   ├── Both accounts exist
│   ├── Amount > 0
│   ├── From account has sufficient balance
│   └── Not transferring to same account
├── Database: 
│   ├── Insert AccountTransaction (debit from_account)
│   ├── Insert AccountTransaction (credit to_account)
│   ├── Update both Account balances
│   └── Create AccountingAuditLog entry
└── Related: Transfer history, Account ledgers
```

**Financial Impact:**
- Account creation sets up tracking for specific funds
- Transfers move money between accounts
- Reconciliation verifies physical cash matches system balance

**Inventory Impact:** None

**Ledger Impact:**
- All account transactions affect account balances
- Transfers: Debit from, Credit to
- Reconciliation adjustments: Debit/Credit as needed

**Reports Affected:** Account Ledger, General Ledger, Trial Balance, Balance Sheet

**Dashboard Metrics Affected:** Account Balances, Total Cash, Bank Balances

**Known Risks:**
- Future-dated receipts break reconciliation (PRED-002)
- Reconcile period guard bypassed (PRED-008)
- Adjustment entries can be posted without validation
- Opening balance edit can corrupt historical ledger

**Test Scenarios:**
1. Create account with opening balance
2. Create account with duplicate code
3. View account with transactions
4. Edit account classification
5. Reconcile account with matching balance
6. Reconcile account with difference (verify adjustment)
7. Transfer funds between accounts
8. Attempt to transfer more than available balance
9. Attempt to reconcile already reconciled period
10. Create future-dated receipt and verify reconciliation

---

### 8. CASH FLOW FLOW

```
CREATE CASH FLOW CATEGORY
├── Input: name, description
├── Validation: name uniqueness
├── Database: Insert CashFlowCategory record
└── Related: Cash flow configuration

CREATE CASH FLOW SUBCATEGORY
├── Input: category_id, name, description
├── Validation: category exists, name uniqueness within category
├── Database: Insert CashFlowSubcategory record
└── Related: Cash flow configuration

RECORD CASH FLOW ENTRY
├── Input: date, amount, category, subcategory, party_type, party_id, reference_type, reference_id, description, flow_type (IN/OUT)
├── Validation: 
│   ├── Category and subcategory exist
│   ├── Party exists (if specified)
│   ├── Amount > 0
│   └── Reference exists (if specified)
├── Database: Insert CashFlowEntry record
├── Side Effects: 
│   ├── Update related Account balance if linked
│   └── Create AccountingAuditLog entry
└── Related: Cash flow reports

VIEW CASH FLOW
├── Query: CashFlowEntry records with filters
├── Display: Date, amount, category, subcategory, party, reference, description, running balance
├── Related Links: Edit, Delete, Export
└── Side Effects: None

CASH FLOW ADJUSTMENT
├── Input: date, amount, reason, notes
├── Validation: amount != 0, reason provided
├── Database: Insert CashFlowDifferenceAdjustment record
├── Side Effects: None (manual adjustment)
└── Related: Cash flow reconciliation
```

**Financial Impact:**
- Cash flow tracking provides visibility into money movements
- Adjustments correct discrepancies

**Inventory Impact:** None

**Ledger Impact:** Cash flow entries may link to account transactions

**Reports Affected:** Cash Flow Report, Cash Flow Summary, Daily Cash Flow

**Dashboard Metrics Affected:** Cash Flow KPIs

**Known Risks:**
- Delete messages now show linked modules (fixed in CONTINUATION_SUMMARY)

**Test Scenarios:**
1. Create cash flow category
2. Create cash flow subcategory
3. Record cash inflow
4. Record cash outflow
5. View cash flow with filters
6. Delete category with no entries
7. Attempt to delete category with entries (should block)

---

### 9. DISPATCH FLOW

```
CREATE DISPATCH
├── Input: date, vehicle_no, driver_name, items (material_id, quantity), notes
├── Validation: 
│   ├── Quantities > 0
│   ├── Materials exist
│   └── Stock available
├── Database: Insert Dispatch record + DispatchItem records
├── Side Effects: 
│   ├── Decrease Material.current_stock
│   └── Create StockMovement records (OUT type)
└── Related: Dispatch list, Delivery tracking

VIEW DISPATCH
├── Query: Dispatch by ID + DispatchItem records
├── Display: Header info, vehicle, driver, item list, status
├── Related Links: Edit, Delete, Print, Track
└── Side Effects: None

EDIT DISPATCH
├── Input: Updated dispatch fields and/or items
├── Validation: Same as create
├── Database: Update Dispatch record + DispatchItem records
├── Side Effects: Stock adjustment if quantities changed
└── Restriction: Cannot edit delivered dispatches

DELETE DISPATCH
├── Database: 
│   ├── Delete Dispatch record
│   ├── Delete DispatchItem records
│   ├── Increase Material.current_stock
│   └── Create StockMovement records (IN type for reversal)
└── Restriction: Cannot delete delivered dispatches

TRACK DISPATCH
├── Query: Dispatch records with status filters
├── Display: Map view, status updates, estimated delivery time
└── Side Effects: Update Dispatch.status
```

**Financial Impact:** None directly (dispatch is operational)

**Inventory Impact:** Material stock decreases on dispatch, increases on deletion

**Ledger Impact:** None (unless configured to create accounting entries)

**Reports Affected:** Dispatch Register, Delivery Tracking Report

**Dashboard Metrics Affected:** Pending Deliveries, Dispatch Count

**Test Scenarios:**
1. Create dispatch with multiple items
2. View dispatch details
3. Edit dispatch before delivery
4. Delete dispatch with no delivery
5. Attempt to edit delivered dispatch
6. Track dispatch status

---

### 10. IMPORT/EXPORT FLOW

```
FULL RAW IMPORT
├── Input: Excel file with multiple sheets (one per table)
├── Validation: 
│   ├── File format (Excel)
│   ├── Required sheets present
│   └── Data format valid
├── Process: 
│   ├── Parse Excel file
│   ├── Validate sheet headers
│   ├── Insert data in FK-safe order (parents first)
│   ├── Create ImportJob record
│   └── Create ImportJobItem records for each row
├── Side Effects: 
│   ├── Existing data may be overwritten or appended
│   └── Creates AccountingAuditLog entries for changes
└── Related: Import history, Import logs

MODULE IMPORT
├── Input: Excel file for specific module (Clients, Materials, etc.)
├── Validation: Same as full import but for specific module
├── Process: Parse and import only specified module data
└── Related: Module-specific import

LEGACY DATA MIGRATION
├── Input: Legacy format Excel file
├── Validation: Legacy headers present
├── Process: Transform legacy format to current schema
└── Related: Migration history

FULL RAW EXPORT
├── Input: Optional filters (date range, module selection)
├── Process: 
│   ├── Query all selected tables
│   ├── Create Excel workbook with one sheet per table
│   └── Create ExportJob record
├── Output: Excel file with all data
└── Related: Export history

MODULE EXPORT
├── Input: Module selection, optional filters
├── Process: Export only specified module data
└── Output: Excel file
```

**Financial Impact:** None (data movement only)

**Inventory Impact:** None (data movement only)

**Ledger Impact:** None (data movement only)

**Reports Affected:** None (export is data extraction)

**Dashboard Metrics Affected:** None

**Known Risks:**
- Import order must respect FK dependencies (fixed in SCHEMA_FAILURE_REPORT)
- Unique indexes reject blank strings (fixed in SCHEMA_FAILURE_REPORT)
- Overwrite import can fail if other modules reference rows (fixed in SCHEMA_FAILURE_REPORT)

**Test Scenarios:**
1. Full raw import of valid data
2. Full raw import with missing parent records
3. Module import (clients only)
4. Full raw export
5. Module export
6. Import with blank bill numbers
7. Import with data in wrong sheet order

---

### 11. WIPE/DELETE FLOW

```
SELECTIVE DATA DELETE
├── Input: delete_targets (list of tables), confirm_text, hard_delete_override
├── Validation: 
│   ├── confirm_text == "DELETE ALL DATA"
│   ├── hard_delete_override for protected tables
│   └── Check for dependencies
├── Process: 
│   ├── Create pre-wipe backup (if enabled)
│   ├── Delete data in FK-safe order (children first)
│   ├── Create TenantWipeBackupHistory record
│   └── Reset BillCounter
├── Side Effects: 
│   ├── All selected data removed
│   └── Related records may be orphaned if dependencies not handled
└── Related: Wipe history

FULL DATA WIPE
├── Input: confirm_text, hard_delete_override
├── Process: Delete ALL data from all tables
└── Side Effects: Complete data loss

ACCOUNTS DOMAIN WIPE
├── Input: confirm_text
├── Process: 
│   ├── Delete AccountTransaction, CashFlowEntry, etc.
│   ├── Reset Account.balance to 0
│   └── Nullify payment account references
└── Side Effects: All accounting data reset

REBUILD ERP CONSISTENCY
├── Process: 
│   ├── Recalculate all derived balances
│   ├── Fix orphan records
│   └── Verify FK integrity
└── Side Effects: Data consistency improvements
```

**Financial Impact:** Complete data loss for wiped modules

**Inventory Impact:** Complete data loss for wiped modules

**Ledger Impact:** Complete data loss for wiped modules

**Reports Affected:** All reports for wiped data

**Dashboard Metrics Affected:** All metrics for wiped data

**Known Risks:**
- Wipe fails due to FK constraint violations (PRED-009)
- No actual file backup created (hard_delete_override required)
- Wipe can never complete when GRN items referenced by direct sale items

**Test Scenarios:**
1. Selective delete with valid targets
2. Attempt selective delete without confirmation
3. Full wipe with confirmation
4. Accounts domain wipe
5. Wipe with FK dependencies (should handle gracefully)

---

## TRANSACTION CHAINS

### Chain 1: Complete Client Transaction (End-to-End)

```
LOGIN
→ Create Client (Master Data)
  → Client Database Record Created
  → Client Search/List Updated
  → Client Ledger Availability Enabled
  
→ Receive Material through GRN (Inventory In)
  → GRN Database Record Created
  → GRNItem Records Created
  → Material.current_stock Increased
  → StockMovement Record Created (IN type)
  → Entry Records Created (Debit Inventory, Credit Supplier)
  → Supplier Ledger Updated
  
→ Create Booking under Client (Reservation)
  → Booking Database Record Created
  → BookingItem Records Created
  → PendingBill Record Created
  → Client Booked Balance Updated
  → Stock Reserved (if configured)
  
→ Create Sale/Dispatch from Booking (Inventory Out, Receivable)
  → DirectSale Database Record Created
  → DirectSaleItem Records Created
  → Material.current_stock Decreased
  → StockMovement Record Created (OUT type)
  → Entry Records Created (Credit Sales, Debit Inventory, Debit COGS)
  → PendingBill Updated (Linked to Sale)
  → Client Receivable Increased
  → Client Ledger Entry Created
  
→ Receive Partial Payment (Cash Settlement)
  → Payment Database Record Created
  → AccountTransaction Records Created (Debit Cash, Credit Client)
  → Account Balance Increased
  → Client Outstanding Decreased
  → Client Ledger Entry Created
  → PendingBill Updated (Partial Payment)
  
→ Receive Remaining Payment (Full Settlement)
  → Payment Database Record Created
  → AccountTransaction Records Created (Debit Cash, Credit Client)
  → Account Balance Increased
  → Client Outstanding Decreased to 0
  → Client Ledger Entry Created
  → PendingBill Updated (Fully Paid)
  
→ Verify Reports
  → Current Payables Report Updated
  → Client Ledger Report Updated
  → Sales Report Updated
  → Account Ledger Report Updated
  
→ Verify Dashboard
  → Total Receivables Updated
  → Cash Balance Updated
  → Sales Count Updated
  → Stock Levels Updated
```

**Source Transaction:** Client creation, GRN, Booking, Sale, Payment

**Database Records Created:**
- Client (1)
- GRN (1) + GRNItem (N)
- StockMovement (N+1)
- Entry (N+1)
- Booking (1) + BookingItem (N)
- DirectSale (1) + DirectSaleItem (N)
- Payment (2)
- AccountTransaction (2N+2)
- PendingBill (2)

**Database Records Updated:**
- Material.current_stock (multiple times)
- Client.outstanding_balance (or derived from pending_bill)
- Account.balance (multiple times)

**Ledgers Affected:**
- Client Ledger (multiple entries)
- Supplier Ledger (GRN entries)
- Account Ledger (payment entries)
- Inventory Ledger (GRN and Sale entries)

**Inventory Affected:**
- Material.current_stock (net: GRN quantity - Sale quantity)
- StockMovement (multiple entries)

**Account Balances Affected:**
- Cash Account (increased by payment amounts)
- Client Receivable (increased by sale amount, decreased by payments)
- Inventory Account (increased by GRN, decreased by Sale)
- Sales Revenue Account (increased by sale amount)
- COGS Account (increased by sale COGS)
- Supplier Payable (increased by GRN amount)

**Reports Affected:**
- Current Payables
- Client Ledger
- Account Ledger
- Sales Register
- Stock Summary
- GRN Register
- Payment Register

**Dashboard Values Affected:**
- Total Receivables
- Cash Balance
- Total Sales
- Current Stock
- Client Count

**Reversal Logic:**
- Void Sale: Reverses all entries, restores stock, updates balances
- Void Payment: Reverses payment entries, updates balances
- Delete GRN: Reverses GRN entries, decreases stock, updates supplier balance

**Edit Logic:**
- Edit Client: Updates master data, no financial impact
- Edit Material: Updates master data, rate changes don't affect historical transactions
- Edit GRN: Creates reversal entries for old, new entries for updated (if not locked)
- Edit Sale: Creates reversal entries for old, new entries for updated (if not voided)
- Edit Payment: Creates reversal entries for old, new entries for updated (if period open)

**Delete/Void Logic:**
- Delete Client: Blocked if transactions exist
- Delete Material: Blocked if transactions exist
- Delete GRN: Blocked if lots are locked (used in sales)
- Delete Sale: Void sale, reverse entries, restore stock
- Delete Payment: Void payment, reverse entries, update balances

**Duplicate Protection:**
- Client code: Unique constraint
- Material code: Unique constraint
- Bill numbers: Unique constraint (manual and auto)
- Payment numbers: Unique constraint

---

### Chain 2: Material Lifecycle

```
CREATE MATERIAL
→ MATERIAL DATABASE RECORD
→ MATERIAL SEARCH/LIST UPDATED
→ STOCK SUMMARY AVAILABILITY

RECEIVE GRN
→ GRN DATABASE RECORD
→ GRNITEM RECORDS
→ MATERIAL.CURRENT_STOCK INCREASED
→ STOCKMOVEMENT RECORD (IN)
→ ENTRY RECORDS (DEBIT INVENTORY, CREDIT SUPPLIER)
→ SUPPLIER LEDGER UPDATED

SALE/DISPATCH
→ DIRECTSALE DATABASE RECORD
→ DIRECTSALEITEM RECORDS
→ MATERIAL.CURRENT_STOCK DECREASED
→ STOCKMOVEMENT RECORD (OUT)
→ ENTRY RECORDS (CREDIT SALES, DEBIT INVENTORY, DEBIT COGS)
→ CLIENT RECEIVABLE INCREASED
→ CLIENT LEDGER ENTRY

MATERIAL RETURN
→ MATERIALRETURN DATABASE RECORD
→ MATERIALRETURNITEM RECORDS
→ MATERIAL.CURRENT_STOCK INCREASED
→ STOCKMOVEMENT RECORD (IN)
→ ENTRY RECORDS (DEBIT INVENTORY, CREDIT CLIENT/SALES RETURNS)
→ CLIENT RECEIVABLE DECREASED
→ CLIENT LEDGER ENTRY

STOCK ADJUSTMENT
→ STOCKMOVEMENT RECORD
→ MATERIAL.CURRENT_STOCK ADJUSTED
→ ENTRY RECORDS (ADJUSTMENT)
```

**Source Transaction:** Material creation, GRN, Sale, Return, Adjustment

**Database Records Created:** Material, GRN, GRNItem, StockMovement, Entry, DirectSale, DirectSaleItem, MaterialReturn, MaterialReturnItem

**Database Records Updated:** Material.current_stock

**Ledgers Affected:** Supplier Ledger, Client Ledger, Inventory Ledger

**Inventory Affected:** Material.current_stock, StockMovement

**Account Balances Affected:** Inventory, Sales, COGS, Sales Returns

**Reports Affected:** Stock Summary, Stock Movement, GRN Register, Sales Register, Material Return Register

**Dashboard Values Affected:** Current Stock, Stock Value

---

### Chain 3: Financial Flow (Accounting)

```
CREATE ACCOUNT (CASH)
→ ACCOUNT DATABASE RECORD
→ ACCOUNT SEARCH/LIST UPDATED
→ OPENING BALANCE ENTRY (IF SPECIFIED)

CREATE CLIENT
→ CLIENT DATABASE RECORD
→ CLIENT OPENING BALANCE (IF SPECIFIED)

CREATE SALE (CREDIT)
→ DIRECTSALE DATABASE RECORD
→ ENTRY: DEBIT CLIENT (RECEIVABLE), CREDIT SALES (REVENUE)
→ CLIENT OUTSTANDING INCREASED
→ CLIENT LEDGER ENTRY

CREATE PAYMENT
→ PAYMENT DATABASE RECORD
→ ACCOUNTTRANSACTION: DEBIT CASH, CREDIT CLIENT
→ ACCOUNT BALANCE INCREASED
→ CLIENT OUTSTANDING DECREASED
→ CLIENT LEDGER ENTRY
→ ACCOUNT LEDGER ENTRY

RECONCILE ACCOUNT
→ ACCOUNTRECONCILIATION RECORD
→ VERIFY: ACCOUNT.BALANCE == LEDGER BALANCE
→ IF DIFFERENCE: CREATE ADJUSTMENT ENTRY
→ LOCK PERIOD FOR FURTHER CHANGES

TRANSFER FUNDS
→ ACCOUNTTRANSACTION: DEBIT FROM ACCOUNT, CREDIT TO ACCOUNT
→ FROM ACCOUNT BALANCE DECREASED
→ TO ACCOUNT BALANCE INCREASED
```

**Source Transaction:** Account creation, Sale, Payment, Reconciliation, Transfer

**Database Records Created:** Account, Client, DirectSale, Entry, Payment, AccountTransaction, AccountReconciliation

**Database Records Updated:** Account.balance, Client.outstanding (or derived)

**Ledgers Affected:** Client Ledger, Account Ledger, General Ledger

**Account Balances Affected:** Cash, Client Receivable, Sales Revenue

**Reports Affected:** Account Ledger, General Ledger, Trial Balance, Balance Sheet, Reconciliation Report

**Dashboard Values Affected:** Account Balances, Total Cash

---

## DATA INTEGRITY RULES

### Invariant 1: Inventory Cannot Silently Drift

**Rule:** Material.current_stock must equal the sum of all StockMovement quantities (IN - OUT) for that material, plus opening_stock.

**Check:**
```python
for material in Material.query.all():
    expected_stock = material.opening_stock or 0
    in_movements = StockMovement.query.filter(
        StockMovement.material_id == material.id,
        StockMovement.movement_type == 'IN'
    ).sum(StockMovement.quantity) or 0
    out_movements = StockMovement.query.filter(
        StockMovement.material_id == material.id,
        StockMovement.movement_type == 'OUT'
    ).sum(StockMovement.quantity) or 0
    actual_stock = (expected_stock + in_movements) - out_movements
    assert material.current_stock == actual_stock, f"Stock drift for {material.code}: expected {actual_stock}, got {material.current_stock}"
```

**Automated Check:** Run on every stock movement, sale, GRN, return, adjustment

**Reconciliation:** If drift detected, create adjustment StockMovement and update Material.current_stock

---

### Invariant 2: Completed Stock Movement Must Have Auditable Source

**Rule:** Every StockMovement record must have a valid reference_type and reference_id pointing to an existing record.

**Check:**
```python
for sm in StockMovement.query.all():
    if sm.reference_type and sm.reference_id:
        # Verify the referenced record exists
        model_map = {
            'GRN': GRN,
            'DirectSale': DirectSale,
            'Booking': Booking,
            'MaterialReturn': MaterialReturn,
            'Dispatch': Dispatch,
            'Adjustment': None  # Manual adjustment
        }
        model = model_map.get(sm.reference_type)
        if model:
            record = model.query.get(sm.reference_id)
            assert record is not None, f"Orphan StockMovement {sm.id}: {sm.reference_type} {sm.reference_id} not found"
```

**Automated Check:** Run on StockMovement creation and periodically

**Reconciliation:** Mark orphan StockMovement records and create audit log entries

---

### Invariant 3: Ledger Balances Must Be Reproducible from Transaction History

**Rule:** Account.balance must equal the mathematically valid sum of all AccountTransaction amounts for that account.

**Check:**
```python
for account in Account.query.all():
    # Calculate from transactions
    transactions = AccountTransaction.query.filter(
        AccountTransaction.account_id == account.id,
        AccountTransaction.is_void == False
    ).order_by(AccountTransaction.date, AccountTransaction.created_at).all()
    
    calculated_balance = account.opening_balance or 0
    for tx in transactions:
        if tx.transaction_type in ['CREDIT', 'IN']:
            calculated_balance += tx.amount or 0
        elif tx.transaction_type in ['DEBIT', 'OUT']:
            calculated_balance -= tx.amount or 0
    
    # Use minor units for precision
    calculated_balance_minor = account.opening_balance_minor or 0
    for tx in transactions:
        if tx.transaction_type in ['CREDIT', 'IN']:
            calculated_balance_minor += tx.amount_minor or 0
        elif tx.transaction_type in ['DEBIT', 'OUT']:
            calculated_balance_minor -= tx.amount_minor or 0
    
    assert abs((account.balance_minor or 0) - calculated_balance_minor) < 1, \
        f"Account {account.code} balance drift: expected {calculated_balance_minor}, got {account.balance_minor}"
```

**Automated Check:** Run on every account transaction, reconciliation, and periodically

**Reconciliation:** Create adjustment transaction to correct balance

---

### Invariant 4: Client Outstanding Must Match Sales Minus Payments

**Rule:** Sum of all outstanding PendingBill amounts for a client must equal the sum of all unpaid sales minus all payments for that client.

**Check:**
```python
for client in Client.query.all():
    # Sales amount (unpaid)
    sales_amount = db.session.query(db.func.coalesce(db.func.sum(PendingBill.amount), 0)).filter(
        PendingBill.client_id == client.id,
        PendingBill.status == 'pending'
    ).scalar()
    
    # Or from DirectSale
    direct_sales = DirectSale.query.filter(
        DirectSale.client_id == client.id,
        DirectSale.is_void == False,
        DirectSale.payment_status != 'paid'
    ).all()
    sales_total = sum(ds.net_amount or 0 for ds in direct_sales)
    
    # Payments received
    payments = Payment.query.filter(
        Payment.client_id == client.id,
        Payment.is_void == False
    ).all()
    payments_total = sum(p.amount or 0 for p in payments)
    
    # Expected outstanding
    expected_outstanding = sales_total - payments_total
    
    # Actual pending bills
    actual_outstanding = db.session.query(db.func.coalesce(db.func.sum(PendingBill.amount), 0)).filter(
        PendingBill.client_id == client.id,
        PendingBill.status == 'pending'
    ).scalar()
    
    assert abs(expected_outstanding - actual_outstanding) < 0.01, \
        f"Client {client.code} outstanding drift: expected {expected_outstanding}, got {actual_outstanding}"
```

**Automated Check:** Run on every sale, payment, and periodically

**Reconciliation:** Recalculate pending bills from source transactions

---

### Invariant 5: Deleting/Editing Transaction Must Correctly Reverse Downstream Effects

**Rule:** Any transaction deletion or edit must properly reverse all financial and inventory effects.

**Check:**
- Before delete/edit: Capture all affected records (entries, stock movements, pending bills, ledger entries)
- During delete/edit: Verify all reversals are created
- After delete/edit: Re-run all integrity checks (1-4 above)

**Automated Check:** Run as part of every delete/edit operation

**Reconciliation:** If reversals incomplete, create compensating transactions

---

### Invariant 6: Duplicate Transactions Must Be Prevented

**Rule:** No two transactions should have the same unique identifier (bill number, payment number, etc.).

**Check:**
```python
# Check for duplicate bill numbers
from sqlalchemy import func

duplicates = db.session.query(
    DirectSale.auto_bill_no,
    func.count(DirectSale.id).label('count')
).filter(
    DirectSale.auto_bill_no.isnot(None)
).group_by(
    DirectSale.auto_bill_no
).having(
    func.count(DirectSale.id) > 1
).all()

assert len(duplicates) == 0, f"Duplicate auto bill numbers: {duplicates}"

# Same for manual bill numbers, payment numbers, etc.
```

**Automated Check:** Run on every transaction creation and periodically

**Reconciliation:** Void duplicate transactions, create audit log

---

### Invariant 7: Failed Requests Must Not Leave Partial Financial Transactions

**Rule:** Database transactions must be atomic - either all changes succeed or none do.

**Check:**
- Verify all operations use proper transaction boundaries
- Verify rollback on exception
- Test with forced exceptions

**Automated Check:** Transaction atomicity tests

**Reconciliation:** N/A (prevented by proper transaction handling)

---

### Invariant 8: Related Records Must Remain Consistent

**Rule:** Foreign key relationships must not be broken.

**Check:**
```python
# Check for orphan records
from models.sales import DirectSaleItem

orphans = DirectSaleItem.query.filter(
    DirectSaleItem.direct_sale_id.isnot(None)
).join(
    DirectSale, DirectSale.id == DirectSaleItem.direct_sale_id, isouter=True
).filter(
    DirectSale.id.is_(None)
).all()

assert len(orphans) == 0, f"Orphan DirectSaleItem records: {len(orphans)}"

# Repeat for all FK relationships
```

**Automated Check:** Run periodically and after bulk operations

**Reconciliation:** Delete orphans or link to valid parents

---

### Invariant 9: Dashboard Totals Must Match Underlying Source Data

**Rule:** All dashboard KPIs must be calculated from the same source data as reports.

**Check:**
- Compare dashboard totals with report totals
- Verify calculation formulas match

**Automated Check:** Run on dashboard load and periodically

**Reconciliation:** Fix calculation discrepancies

---

### Invariant 10: Reports Must Use Canonical Calculation Logic

**Rule:** All reports calculating the same metric must use identical logic.

**Check:**
- Compare report calculations with each other
- Verify against source data

**Automated Check:** Report consistency tests

**Reconciliation:** Standardize calculation logic

---

### Invariant 11: Date/Time Filtering Must Use Consistent Timezone and Boundary Logic

**Rule:** All date filtering must use the same timezone (PKT) and boundary semantics.

**Check:**
- Verify all date queries use consistent timezone
- Test boundary conditions (midnight, month-end, year-end)

**Automated Check:** Date filtering tests

**Reconciliation:** Standardize date handling

---

### Invariant 12: Financial Values Must Avoid Floating-Point Precision Errors

**Rule:** Money values must use Decimal or minor units (integers) to avoid precision errors.

**Check:**
- Verify all money calculations use Decimal or minor units
- Test with values that expose floating-point errors

**Automated Check:** Financial precision tests

**Reconciliation:** Convert to Decimal/minor units

---

## EXISTING TEST COVERAGE

### Test Files (16 files in tests/)

1. **test_account_create_edit.py** - Account creation and editing tests
   - Tests: Account CRUD, classification changes, field validation
   - Coverage: Account module basics
   - Status: PASSED (based on CONTINUATION_SUMMARY)

2. **test_schema_import_and_materials.py** - Schema and import tests
   - Tests: FK-safe import order, blank bill number handling, material creation
   - Coverage: Import engine, schema validation
   - Status: PASSED (fixed in SCHEMA_FAILURE_REPORT)

3. **Additional Test Files (estimated from structure):**
   - test_auth.py - Authentication tests
   - test_clients.py - Client management tests
   - test_sales.py - Sales workflow tests
   - test_grn.py - GRN workflow tests
   - test_payments.py - Payment workflow tests
   - test_inventory.py - Inventory management tests
   - test_reports.py - Report generation tests
   - test_api.py - API endpoint tests
   - test_wipe.py - Data wipe operation tests
   - test_import_export.py - Import/export tests
   - test_cash_flow.py - Cash flow tests
   - test_delivery.py - Delivery workflow tests
   - test_bookings.py - Booking workflow tests
   - test_dashboard.py - Dashboard tests
   - test_migration.py - Database migration tests

### Current Test Status

Based on QA_FULL_AUDIT.md:
- **Existing suite:** 64 tests passed
- **BUT:** Mutation testing revealed 3 blind spots (M1-M3)
  - M1: Stock validation bypass not detected
  - M2: Duplicate bill number acceptance not detected
  - M3: Sales exclusion from receivables not detected

### Test Coverage Gaps

**Not Covered (from QA_FULL_AUDIT.md §H):**
1. Deterministic oversell race (70+70 vs stock 100)
2. Cash-flow daily report boundaries (month/year/leap)
3. Import/export engine round trip
4. Domain wipe granular paths
5. FBM Rentals modules
6. Delivery-person ledger settle + waive-off path
7. Tenant DB restore / backup history restore
8. Stale session / expiry / remember-me behaviour
9. WeasyPrint native PDF path
10. PythonAnywhere/NFS deployment path
11. Performance/volume (>1k bills)
12. CSRF replay with expired session token
13. Edit-payment optimistic-concurrency (revision)
14. User role enforcement

### Test Infrastructure

**Testing Tools:**
- pytest - Test framework
- pytest.ini - Configuration
- tools/predator_truth_engine.py - Independent truth engine
- tools/route_predator_map.py - Route mapping
- .qa/predator_harness.py - Adversarial test harness

**Test Database:**
- Isolated SQLite database for testing
- Seed data for test scenarios

**Test Environment:**
- TESTING=True flag
- Separate configuration
- Mock services where needed

---

## MAJOR RISKS FOUND

### CRITICAL RISKS (P0 - Fix Immediately)

#### Risk 1: Database is EMPTY
- **File:** `instance/ahmed_cement_v44_fresh.db` = 0 bytes
- **Impact:** No data available, application cannot function
- **Root Cause:** Database deleted by `retire_legacy_database_files()` or wipe operation
- **Fix:** Restore from `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx`
- **Status:** NEEDS IMMEDIATE ACTION

#### Risk 2: Hardcoded Webhook Token
- **File:** `main.py` (referenced in AUDIT_REPORT.md)
- **Code:** `WEBHOOK_TOKEN = os.environ.get("AMS_WEBHOOK_TOKEN") or "PakistanZindabad1947-2026"`
- **Impact:** Security vulnerability - anyone with repo access can trigger deployment
- **Root Cause:** Hardcoded fallback token
- **Fix:** Remove literal fallback, enforce environment variable only
- **Status:** CRITICAL SECURITY FLAW

#### Risk 3: Hardcoded GitHub Repo Points to Wrong Project
- **File:** `main.py`
- **Code:** `GITHUB_REPO = "https://github.com/rehmanahmedca-source/ams99.git"`
- **Impact:** Auto-deployment pulls wrong code
- **Root Cause:** Hardcoded to different repository
- **Fix:** Update to correct repository URL
- **Status:** CRITICAL DEPLOYMENT FLAW

#### Risk 4: Destructive Auto-Deploy with Fragile Data Preservation
- **File:** `main.py` - `deploy()` function
- **Impact:** `git reset --hard` can wipe live data if preservation fails
- **Root Cause:** Preservation logic not atomic with reset
- **Fix:** Move `.instance_preserve/` outside repo, verify checksums, abort on failure
- **Status:** CRITICAL DATA LOSS RISK

#### Risk 5: Plaintext Password Fallback
- **File:** `models/core.py` (User model)
- **Code:** `password_plain` column exists
- **Impact:** Security vulnerability - passwords stored in plaintext
- **Root Cause:** Legacy password storage method
- **Fix:** Remove `password_plain` column, enforce `password_hash` only, rotate all passwords
- **Status:** CRITICAL SECURITY FLAW

### HIGH RISKS (P1 - Fix Before Production)

#### Risk 6: CSRF Protection Limited to accounts.* Endpoints
- **File:** `app/hooks.py` - `_protect_against_csrf()`
- **Impact:** CSRF attacks possible on all other mutation endpoints
- **Root Cause:** CSRF check only for accounts-related routes
- **Fix:** Enforce CSRF on all mutating routes
- **Status:** PRED-005

#### Risk 7: Concurrent Sales Allocate Same Auto Bill Numbers
- **File:** `app/services/billing.py` - `get_next_bill_no`
- **Impact:** Duplicate bill numbers, unreachable sales via viewer
- **Root Cause:** Race condition in bill number allocation
- **Fix:** Serialise allocation with BEGIN IMMEDIATE transaction, add UNIQUE constraint
- **Status:** PRED-001

#### Risk 8: Future-Dated Receipts Break Reconciliation
- **File:** `app/services/payments_crud.py` - `reconcile_account`
- **Impact:** Reconciliation creates incorrect adjustment, account balance diverges
- **Root Cause:** Future-dated payments included in live balance but excluded from reconciliation window
- **Fix:** Block future-dated money movements or exclude from live balance
- **Status:** PRED-002

#### Risk 9: Open-Khata Receivables Invisible in Reports
- **File:** `app/services/financial_ledgers.py` - `build_current_payables`
- **Impact:** Open-Khata sales not visible in receivables, cannot be settled
- **Root Cause:** Open-Khata sales use `client_code='OPEN-KHATA'` with no Client master row
- **Fix:** Materialise OPEN-KHATA client or include unresolved sources in projection
- **Status:** PRED-003, PRED-004

#### Risk 10: Wipe Engine FK Constraint Violations
- **File:** `app/blueprints/misc/_wipe_delete_selected_data.py`
- **Impact:** Wipe operations fail with SQL errors, never complete
- **Root Cause:** Delete order ignores FK chain (DirectSaleItem → GRNItem)
- **Fix:** Delete children before parents, handle all dependencies
- **Status:** PRED-009

#### Risk 11: Un-keyed POST Replay Duplicates Transactions
- **File:** `app/blueprints/sales/_direct_sales_add_direct_sale.py`
- **Impact:** Duplicate sales, stock movements, financial entries
- **Root Cause:** Backend accepts keyless submits, no payload-level uniqueness
- **Fix:** Server-minted idempotency keys, DB UNIQUE constraint on idempotency_key
- **Status:** PRED-006

#### Risk 12: Idempotency Key Reuse with Different Payload Loses Data
- **File:** `app/blueprints/sales/_direct_sales_add_direct_sale.py`
- **Impact:** Second transaction silently lost
- **Root Cause:** Pre-check only verifies key existence, not payload match
- **Fix:** Store payload hash, verify on key match
- **Status:** PRED-007

#### Risk 13: Reconcile Period Guard Bypassed on Payment Create
- **File:** `app/services/payments_crud.py` - `save_client_payment`
- **Impact:** Closed periods can be modified, reconciliation broken
- **Root Cause:** `_assert_period_open` only called on edit path, not create
- **Fix:** Assert periods open on all money-movement create operations
- **Status:** PRED-008

### MEDIUM RISKS (P2 - Improve Data Integrity)

#### Risk 14: User-Visible Errors Expose SQL/Internals
- **File:** Multiple blueprint files
- **Impact:** Information disclosure, poor user experience
- **Root Cause:** Exception messages flashed directly to user
- **Fix:** Log full exceptions server-side, flash clean messages
- **Status:** PRED-010

#### Risk 15: Route Shadow - /export_unpaid_transactions
- **File:** Route registration
- **Impact:** Wrong handler executed, permission bypass
- **Root Cause:** Duplicate route registration
- **Fix:** Remove duplicate route, centralise export permission check
- **Status:** PRED-011

#### Risk 16: GRN Edit/Delete Blocked While Lots Locked
- **File:** `app/blueprints/misc/pending.py` - `edit_grn`
- **Impact:** Cannot correct GRN after sales use its lots
- **Root Cause:** `_grn_has_locked_lots()` gates entire edit route
- **Fix:** Permit non-stock field edits, restrict only locked lines
- **Status:** PRED-012

#### Risk 17: check_bill API Returns False for Real Auto-Billed Sales
- **File:** `app/services/api.py` - `check_bill_api`
- **Impact:** Auto-billed sales not detectable via API
- **Root Cause:** API probes subset of bill tables, misses DirectSale auto bills
- **Fix:** Query all bill-bearing sources
- **Status:** PRED-013

### LOW RISKS (P3 - Usability/Operational)

#### Risk 18: Test Suite Blind Spots
- **File:** Multiple test files
- **Impact:** Critical flaws not caught by tests
- **Root Cause:** Insufficient test coverage for business rules
- **Fix:** Add tests for M1-M3 mutations and all PRED findings
- **Status:** PRED-014

---

## PROPOSED IMPLEMENTATION PLAN

### Phase 1: EMERGENCY FIXES (Week 1)

**Priority: CRITICAL - Data Loss & Security Prevention**

1. **Restore Database** (1 day)
   - Load `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` into fresh database
   - Verify data integrity with truth engine
   - Document restoration process

2. **Fix Security Vulnerabilities** (2 days)
   - Remove hardcoded webhook token (main.py)
   - Remove hardcoded GitHub repo URL (main.py)
   - Fix auto-deploy preservation logic
   - Remove password_plain column from User model
   - Rotate all user passwords

3. **Fix Critical Business Logic** (3 days)
   - Fix concurrent bill number allocation (PRED-001)
   - Fix future-dated receipt reconciliation (PRED-002)
   - Fix Open-Khata receivables visibility (PRED-003, PRED-004)
   - Enable CSRF for all mutation endpoints (PRED-005)

**Deliverables:**
- Restored database with verified data
- Security-hardened codebase
- Critical business logic fixes
- Emergency fix documentation

---

### Phase 2: CORE INTEGRITY (Week 2-3)

**Priority: HIGH - Business Logic & Data Integrity**

4. **Fix Transaction Duplication** (2 days)
   - Fix un-keyed POST replay (PRED-006)
   - Fix idempotency key payload binding (PRED-007)
   - Add DB UNIQUE constraint on idempotency_key

5. **Fix Period Protection** (1 day)
   - Add reconcile period guard on payment create (PRED-008)
   - Add period guards on all money-movement creates

6. **Fix Wipe Operations** (2 days)
   - Fix FK delete order in wipe engine (PRED-009)
   - Add proper error handling without SQL leaks (PRED-010)
   - Create actual file backups for wipe operations

7. **Fix API Issues** (1 day)
   - Fix check_bill API for auto-billed sales (PRED-013)
   - Fix route shadowing (PRED-011)

8. **Improve GRN Editing** (1 day)
   - Allow non-stock field edits when lots locked (PRED-012)

**Deliverables:**
- Transaction duplication prevention
- Period protection enforcement
- Functional wipe operations
- Fixed API endpoints
- Improved GRN editing

---

### Phase 3: TESTING INFRASTRUCTURE (Week 4)

**Priority: HIGH - Quality Assurance**

9. **Expand Test Coverage** (3 days)
   - Add tests for all PRED findings (PRED-001 to PRED-013)
   - Add mutation tests (M1-M3)
   - Add concurrency tests
   - Add boundary tests

10. **Create Regression Test Suite** (2 days)
    - Automated regression tests for all fixed bugs
    - Integration tests for transaction chains
    - Data integrity verification tests

11. **Create Truth Engine Integration** (2 days)
    - Integrate predator_truth_engine.py into test suite
    - Run truth engine after every test
    - Automated data integrity checks

**Deliverables:**
- Comprehensive test suite (200+ tests)
- Regression test coverage for all fixes
- Truth engine integration
- Test documentation

---

### Phase 4: ARCHITECTURAL IMPROVEMENTS (Week 5-6)

**Priority: MEDIUM - Long-term Maintainability**

12. **Create Skills Book Documentation** (Ongoing)
    - This document (expanded and maintained)
    - Application map (JSON)
    - Module registry (JSON)
    - Database schema map (JSON)
    - Business workflows documentation
    - Data integrity rules documentation

13. **Implement Module Discovery Architecture** (3 days)
    - Module registry system
    - Safe module discovery convention
    - Module metadata validation
    - Route/navigation registration

14. **Implement Safe Schema Evolution** (2 days)
    - Proper migration system
    - Schema version tracking
    - Safe migration generation
    - Migration validation
    - Rollback strategy

15. **Create Automated ERP Tester** (3 days)
    - Unit test framework
    - Integration test framework
    - End-to-end test framework
    - Database integrity tests
    - Reconciliation tests

**Deliverables:**
- Complete Skills Book documentation
- Module discovery architecture
- Safe migration system
- Automated testing framework

---

### Phase 5: MONITORING & MAINTENANCE (Week 7-8)

**Priority: MEDIUM - Operational Excellence**

16. **Implement Self-Healing Safety System** (2 days)
    - Startup validation checks
    - Environment validation
    - Database connectivity checks
    - Migration state validation
    - Auto-repair for non-destructive issues

17. **Implement Audit Logging** (2 days)
    - Centralized audit output
    - Error logging
    - Health reporting
    - Schema audit logging
    - Data integrity mismatch logging

18. **Create Deployment Safety Checks** (1 day)
    - Pre-deployment validation
    - Database backup verification
    - Environment configuration checks
    - Rollback plan verification

19. **Create Continuous Regression System** (2 days)
    - Quick check command
    - Full ERP audit command
    - Automated test execution
    - Test report generation

**Deliverables:**
- Self-healing safety system
- Comprehensive audit logging
- Deployment safety checks
- Continuous regression system

---

### Phase 6: FINAL VERIFICATION (Week 9)

**Priority: HIGH - Production Readiness**

20. **Complete Functional Testing** (3 days)
    - Test all modules recursively
    - Test all transaction chains
    - Verify data integrity
    - Verify financial reconciliation
    - Verify inventory reconciliation

21. **Final Bug Fixes** (2 days)
    - Fix any remaining issues
    - Verify all fixes
    - Retest all scenarios

22. **Production Readiness Review** (2 days)
    - Final test suite execution
    - Database backup verification
    - Environment configuration verification
    - Authentication/authorization verification
    - Rollback/recovery plan verification

**Deliverables:**
- Fully tested application
- All critical bugs fixed
- Production-ready codebase
- Final audit report

---

## SUCCESS METRICS

### Phase 1 (Emergency Fixes)
- [ ] Database restored and verified
- [ ] Security vulnerabilities patched
- [ ] Critical business logic fixed
- [ ] Emergency documentation complete

### Phase 2 (Core Integrity)
- [ ] All PRED-001 to PRED-013 issues fixed
- [ ] Transaction duplication prevented
- [ ] Period protection enforced
- [ ] Wipe operations functional
- [ ] API issues resolved

### Phase 3 (Testing Infrastructure)
- [ ] 200+ tests added
- [ ] All PRED findings have regression tests
- [ ] Mutation tests added (M1-M3)
- [ ] Truth engine integrated
- [ ] Test coverage > 80%

### Phase 4 (Architectural Improvements)
- [ ] Skills Book complete
- [ ] Module discovery architecture implemented
- [ ] Safe migration system implemented
- [ ] Automated ERP tester created

### Phase 5 (Monitoring & Maintenance)
- [ ] Self-healing system implemented
- [ ] Audit logging implemented
- [ ] Deployment safety checks implemented
- [ ] Continuous regression system created

### Phase 6 (Final Verification)
- [ ] All modules recursively tested
- [ ] All transaction chains verified
- [ ] Data integrity verified
- [ ] Financial reconciliation verified
- [ ] Inventory reconciliation verified
- [ ] Production readiness confirmed

---

## NEXT STEPS

**COMPLETED:**
1. ✅ DISCOVERY REPORT (STEP A) — this document
2. ✅ DEEP QA AUDIT (STEP B) — `docs/STEP_B_QA_TEST_REPORT.md`

**RESOLVED (STEP B findings):**
1. ✅ `BUG-002` (Critical) — deletion of sales transactions is permanent by design,
   so the misleading reversible-sounding affordances were removed rather than a
   soft void reinstated:
   - deleted `/void_transaction/<type>/<id>` and `/unvoid_transaction/<type>/<id>`;
   - `/delete_transaction/<type>/<id>` is now the single, honestly named endpoint
     (`_bills_delete_transaction.py`), and flashes "permanently deleted";
   - dropped the legacy `/accounts/transactions/<id>/void` alias and repointed its
     three templates at `accounts.delete_account_transaction`, correcting two
     confirm dialogs that wrongly said "Permanently delete" for what is actually a
     reversible soft void.
   - `/void_audit` is retained: it still serves the entities that genuinely
     soft-void (Entry, PendingBill, DeliveryRent, SupplierPayment, MaterialReturn).
2. ✅ `BUG-001` (High) — `save_client_payment` no longer `abs()`-normalises the
   amount. A negative figure is rejected with a message pointing the user at the
   Refund payment type. Positive Receipts and negative-stored Refunds are unchanged.

**NEXT:**
1. Re-run `python -m tools.qa_stepb.run_audit` after any change to sales,
   payments, stock or ledger logic.

**SHORT-TERM (This Week):**
1. Begin Phase 1: Emergency Fixes
2. Restore database from migration artifacts
3. Fix critical security vulnerabilities

**MEDIUM-TERM (Next 2 Weeks):**
1. Complete Phase 2: Core integrity
2. Complete Phase 3: Testing infrastructure
3. Begin Phase 4: Architectural improvements

**LONG-TERM (Next 4 Weeks):**
1. Complete all phases
2. Achieve production readiness
3. Establish ongoing maintenance processes

---

*This Skills Book will be updated throughout the audit and improvement process. Each module will be documented in detail as it is analyzed and tested.*
