"""
Verify the page routing logic is correct for all known simulator events.
Run this locally: python tmp_verify_routing.py
"""
import sys
sys.path.insert(0, '.')

from api.page_map import normalize_event, resolve_page

KNOWN_PAGES = {
    "/register", "/login", "/dashboard", "/accounts", "/payees",
    "/transactions", "/loans", "/pro-feature?id=crypto-trading",
    "/pro-feature?id=ai-insights", "/pro-feature?id=wealth-management-pro",
    "/pro-feature?id=bulk-payroll-processing", "/profile"
}

# All events the simulator emits (from eventRoutes.ts)
test_events = [
    # Auth
    ("free.auth.register.success",     "/register"),
    ("free.auth.login.success",        "/login"),
    # Dashboard
    ("free.dashboard.view",            "/dashboard"),
    ("pro.features.view",              "/dashboard"),
    ("pro.features.unlock_success",    "/dashboard"),
    # Accounts
    ("free.accounts.view",             "/accounts"),
    # Transactions
    ("free.transactions.view",         "/transactions"),
    ("free.payment.success",           "/transactions"),
    ("free.payment.failed",            "/transactions"),
    # Payees
    ("free.payees.view",               "/payees"),
    ("free.payees.add_success",        "/payees"),
    ("free.payees.search",             "/payees"),
    ("free.payees.remove",             "/payees"),
    ("free.payees.edit",               "/payees"),
    # Loans
    ("free.loan.kyc_started",          "/loans"),
    ("free.loan.kyc_completed",        "/loans"),
    ("free.loan.kyc_failed",           "/loans"),
    ("lending.loans.viewed",           "/loans"),
    ("lending.loan.applied",           "/loans"),
    ("lending.loan.kyc_completed",     "/loans"),
    ("lending.loan.kyc_abandoned",     "/loans"),
    # Pro features
    ("pro.crypto_trade_execution.success",  "/pro-feature?id=crypto-trading"),
    ("pro.crypto_trade_execution.failed",   "/pro-feature?id=crypto-trading"),
    ("pro.crypto_portfolio.view",           "/pro-feature?id=crypto-trading"),
    ("pro.wealth_rebalance.success",        "/pro-feature?id=wealth-management-pro"),
    ("pro.wealth_insights.view",            "/pro-feature?id=wealth-management-pro"),
    ("pro.payroll_batch.success",           "/pro-feature?id=bulk-payroll-processing"),
    ("pro.finance-library.book_access",     "/pro-feature?id=ai-insights"),
    # Location (should be dashboard)
    ("location_captured",                   "/dashboard"),
]

print(f"{'RAW EVENT':<45} {'NORMALIZED':<55} {'PAGE':<45} {'EXPECTED':<45} {'OK?'}")
print("-" * 240)

all_ok = True
for raw, expected in test_events:
    normalized = normalize_event(raw)
    page = resolve_page(normalized) or resolve_page(raw) or "/dashboard"
    if page not in KNOWN_PAGES:
        page = "/dashboard"
    ok = "✅" if page == expected else "❌ FAIL"
    if page != expected:
        all_ok = False
    print(f"{raw:<45} {normalized:<55} {page:<45} {expected:<45} {ok}")

print()
print("ALL OK!" if all_ok else "FAILURES DETECTED - FIX NEEDED")
