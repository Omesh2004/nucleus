"""
page_map.py — Centralised Feature → Page mapping for NexaBank & SafexBank.

Taxonomy convention: [page_name].[feature].[status]
  - page_name: dashboard | accounts | transactions | payees | loans | pro-feature | profile | admin_loans | admin_feature_toggles | admin_simulate | login | register
  - feature: overview | open_account | transfer_money | filter_by_date | add_payee | crypto-trading | etc...
  - status:  view | success | error | failed | action

"""

from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# TAXONOMY PAGE TO URL MAPPING
# Maps the taxonomy root [page_name] to the actual frontend URL.
# ─────────────────────────────────────────────────────────────────────────────
URL_MAP: dict[str, str] = {
    "dashboard": "/dashboard",
    "accounts": "/accounts",
    "transactions": "/transactions",
    "payees": "/payees",
    "loans": "/loans",
    "lending": "/loans",
    "pro-feature": "/pro-features",
    "profile": "/profile",
    "admin_loans": "/admin/loans",
    "admin_feature_toggles": "/admin/feature-toggles",
    "admin_simulate": "/admin/simulate",
    "login": "/login",
    "register": "/register",
    "core": "/dashboard",

    # SafexBank additions
    "transfers": "/transfers",
    "approvals": "/approvals",
    "cards": "/cards",
}

# ─────────────────────────────────────────────────────────────────────────────
# LEGACY FALLBACK MAP
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_PAGE_MAP: dict[str, str] = {
    "login":                        "/login",
    "register":                     "/register",
    "dashboard_view":               "/dashboard",
    "page_view":                    "/dashboard",
    "view_dashboard":               "/dashboard",
    "accounts_view":                "/accounts",
    "transactions_view":            "/transactions",
    "payment_completed":            "/transactions",
    "payees_view":                  "/payees",
    "payee_added":                  "/payees",
    "loan_applied":                 "/loans",
    "loans_page_view":              "/loans",
    "crypto-trading":               "/pro-feature?id=crypto-trading",
    "crypto_trade_execution":       "/pro-feature?id=crypto-trading",
    "wealth-management-pro":        "/pro-feature?id=wealth-management-pro",
    "wealth_rebalance":             "/pro-feature?id=wealth-management-pro",
    "bulk-payroll-processing":      "/pro-feature?id=bulk-payroll-processing",
    "payroll_batch_processed":      "/pro-feature?id=bulk-payroll-processing",
    "ai-insights":                  "/pro-feature?id=ai-insights",
    "pro_book_download":            "/pro-feature?id=ai-insights",
    "pro_book_read":                "/pro-feature?id=ai-insights",
    "kyc_completed":                "/loans",
    "kyc_started":                  "/loans",
    "transfer_funds":               "/accounts",
    "feature_view":                 "/dashboard",
    "pro_feature_usage":            "/dashboard",
    "location_captured":            "/dashboard",
}

# ─────────────────────────────────────────────────────────────────────────────
# MULTI-PAGE EVENTS
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_MULTI_PAGE: dict[str, list[str]] = {
    "payees.pay_now.success":     ["/payees", "/transactions"],
    "loans.kyc.success":          ["/loans", "/profile"],
}

# ─────────────────────────────────────────────────────────────────────────────
# HUMAN-READABLE DISPLAY NAMES
# Maps explicit event paths to highly readable dashboard names.
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_DISPLAY_NAMES: dict[str, str] = {
    # Dashboard
    "dashboard.page.view": "Dashboard View",
    "dashboard.overview.view": "Dashboard Overview",
    "dashboard.analytics_tab.view": "Dashboard Analytics",

    # Accounts
    "accounts.page.view": "Accounts View",
    "accounts.open_account.success": "Account Opened",
    "accounts.transfer_money.success": "Transfer Success",
    "accounts.transfer_money.error": "Transfer Error",

    # Transactions
    "transactions.page.view": "Transactions View",
    "transactions.filter_by_date.success": "Transactions Filtered",
    "transactions.history.view": "Transaction History Viewed",
    "transactions.search_transactions.success": "Transaction Searched",

    # Payees
    "payees.page.view": "Payees View",
    "payees.add_payee.success": "Payee Added",
    "payees.edit_payee.success": "Payee Edited",
    "payees.remove_payee.success": "Payee Removed",
    "payees.search_payee.success": "Payee Searched",
    "payees.copy_account_number.success": "Account Copied",
    "payees.pay_now.success": "Payment Success",

    # Loans
    "loans.page.view": "Loans View",
    "loans.emi_estimator.action": "EMI Calculated",
    "loans.apply_loan.action": "Loan Flow Started",
    "loans.proceed_to_kyc.action": "Loan KYC Started",
    "loans.submit_application.success": "Loan App Submitted",
    "loans.submit_application.error": "Loan App Error",

    # Pro Features
    "pro-feature.ai-insights.read_online": "AI Insights Read",
    "pro-feature.crypto-trading.buy_success": "Crypto Bought",
    "pro-feature.crypto-trading.sell_success": "Crypto Sold",
    "pro-feature.wealth-management-pro.insights_view": "Wealth Insights",
    "pro-feature.wealth-management-pro.rebalance_success": "Wealth Rebalanced",
    "pro-feature.bulk-payroll-processing.search_by_name": "Payroll Searched",
    "pro-feature.bulk-payroll-processing.add_payee": "Payroll Payee Added",
    "pro-feature.bulk-payroll-processing.pay_all_success": "Bulk Payroll Success",

    # Profile
    "profile.page.view": "Profile View",
    "profile.edit_details.success": "Profile Details Edited",

    # Auth
    "login.page.view": "Login Page",
    "login.auth.success": "Login Success",
    "login.auth.error": "Login Error",
    "register.page.view": "Register Page",
    "register.auth.success": "Register Success",
    "register.auth.error": "Register Error",

    # Admin
    "admin_loans.page.view": "Admin Loans View",
    "admin_loans.view_details.view": "Admin Loan Details",
    "admin_loans.approve.success": "Loan Approved",
    "admin_loans.reject.success": "Loan Rejected",
    "admin_feature_toggles.page.view": "Admin Toggles View",
    "admin_feature_toggles.toggle_feature.success": "Feature Toggled",
    "admin_simulate.page.view": "Admin Simulator View",
    "admin_simulate.run_simulation.success": "Simulation Run",

    # Additional Explicit Mappings requested by user
    "loans.kyc.abandoned": "KYC Abandoned (View)",
    "loans.kyc.completed": "KYC Completed",
    "loans.kyc.started": "KYC Started",
    "dashboard.pro_catalog.view": "Pro Unlocked (View)",
    "dashboard.pro_license.unlocked": "Pro License Unlocked (View)",
    "dashboard.location.captured": "Location Captured (View)",
    "dashboard.feature.usage": "Pro Feature Engagement",
    "dashboard.feature.view": "Feature Catalog View",
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def normalize_event(event_name: str) -> str:
    """
    Centralized normalization layer.
    Maps all raw events into canonical feature IDs.
    Format: [domain].[feature].[status]
    """
    if not event_name:
        return "core.unknown.view"
        
    e = event_name.lower().strip()
    
    mapping = {
        # General Views
        "page_view": "dashboard.page.view",
        "dashboard_view": "dashboard.page.view",
        "view_dashboard": "dashboard.page.view",
        "feature_view": "dashboard.feature.view",
        "pro_feature_usage": "dashboard.feature.usage",
        "location_captured": "dashboard.location.captured",
        "free.dashboard.view": "dashboard.page.view",

        # Accounts
        "accounts_view": "accounts.page.view",
        "transfer_funds": "accounts.transfer_money.success",
        "free.accounts.view": "accounts.page.view",

        # Transactions
        "transactions_view": "transactions.page.view",
        "payment_completed": "transactions.pay_now.success",
        "payment_failed": "transactions.pay_now.failed",
        "free.transactions.view": "transactions.page.view",
        "free.payment.success": "transactions.pay_now.success",

        # Payees
        "payees_view": "payees.page.view",
        "payees": "payees.pay_now.success",
        "payee_added": "payees.add_payee.success",
        "free.payees.view": "payees.page.view",
        "free.payees.search": "payees.search_payee.success",
        "free.payees.add_success": "payees.add_payee.success",
        "free.payees.remove": "payees.remove_payee.success",
        "free.payees.edit": "payees.edit_payee.success",

        # Loans
        "loan_applied": "loans.apply_loan.action",
        "loans_page_view": "loans.page.view",
        "kyc_completed": "loans.kyc.completed",
        "kyc_started": "loans.kyc.started",
        "kyc_abandoned": "loans.kyc.abandoned",
        "free.loan.kyc_started": "loans.kyc.started",
        "free.loan.kyc_completed": "loans.kyc.completed",
        "free.loan.kyc_failed": "loans.kyc.abandoned",
        "lending.loans.viewed": "loans.page.view",
        "lending.loan.applied": "loans.apply_loan.action",
        "lending.loan.kyc_completed": "loans.kyc.completed",
        "lending.loan.kyc_abandoned": "loans.kyc.abandoned",

        # Pro Features
        "pro.features.view": "dashboard.pro_catalog.view",
        "pro.features.unlock_success": "dashboard.pro_license.unlocked",
        "pro.crypto_trade_execution.success": "pro-feature.crypto-trading.buy_success",
        "pro.crypto_trade_execution.failed": "pro-feature.crypto-trading.buy_error",
        "pro.wealth_rebalance.success": "pro-feature.wealth-management-pro.rebalance_success",
        "pro.payroll_batch.success": "pro-feature.bulk-payroll-processing.pay_all_success",
        "pro.wealth_insights.view": "pro-feature.wealth-management-pro.insights_view",
        "pro.crypto_portfolio.view": "pro-feature.crypto-trading.page_view",
        "pro.finance-library.book_access": "pro-feature.ai-insights.read_online",

        # Pro Legacy names
        "payroll_batch_processed": "pro-feature.bulk-payroll-processing.pay_all_success",
        "crypto_trade_execution": "pro-feature.crypto-trading.buy_success",
        "wealth_rebalance": "pro-feature.wealth-management-pro.rebalance_success",
        "crypto-trading": "pro-feature.crypto-trading.page_view",
        "wealth-management-pro": "pro-feature.wealth-management-pro.insights_view",
        "bulk-payroll-processing": "pro-feature.bulk-payroll-processing.page_view",
        "ai-insights": "pro-feature.ai-insights.read_online",
        "pro_book_read": "pro-feature.ai-insights.read_online",
        "pro_book_download": "pro-feature.ai-insights.read_online",

        # Auth
        "free.auth.login.success": "login.auth.success",
        "free.auth.register.success": "register.auth.success",
    }
    
    if e in mapping:
        return mapping[e]

    # 2. Heuristic suffix normalizations
    if e.endswith("_success"):
        e = e.replace("_success", ".success")
    elif e.endswith("_failed"):
        e = e.replace("_failed", ".failed")
    elif e.endswith("_view"):
        e = e.replace("_view", ".view")

    # 3. Taxonomy enforcer
    if "." not in e:
        e = f"core.{e}.view"
        
    return e

def resolve_page(event_name: str) -> Optional[str]:
    """
    Resolve an event_name to its canonical page URL.
    Parses strict taxonomy [page_name].[feature].[status] directly.
    """
    if not event_name:
        return None

    # 1. Check legacy/explicit map
    if event_name in FEATURE_PAGE_MAP:
        return FEATURE_PAGE_MAP[event_name]

    # 2. Strict taxonomy parsing
    parts = event_name.split(".")
    if len(parts) >= 2:
        page_root = parts[0]
        
        # Handle dynamic query param pro-feature URL routing
        if page_root == "pro-feature" and len(parts) >= 3:
            return f"/pro-feature?id={parts[1]}"
            
        # Map roots like admin_loans to /admin/loans
        if page_root in URL_MAP:
            return URL_MAP[page_root]
        
        # Fallback to direct routing for unknown roots
        clean_root = page_root.replace('_', '/')
        return f"/{clean_root}"

    # 3. Give up -> will be filed under /_other
    return None


def resolve_display_name(event_name: str) -> str:
    """
    Returns a human-readable display name for an event.
    Automatically formats unknown taxonomy paths beautifully.
    """
    if not event_name:
        return "Unknown"

    name = FEATURE_DISPLAY_NAMES.get(event_name)
    if name:
        return name

    # Fallback formatter: page.feature.status -> "Page: Feature (Status)"
    parts = event_name.replace("-", "_").split(".")
    if len(parts) == 3:
        page_name = parts[0].replace("_", " ").title()
        feature = parts[1].replace("_", " ").title()
        status = parts[2].title()
        return f"{page_name}: {feature} ({status})"
    elif len(parts) == 2:
        page_name = parts[0].replace("_", " ").title()
        feature = parts[1].replace("_", " ").title()
        return f"{page_name}: {feature}"

    return event_name.replace("_", " ").title()
