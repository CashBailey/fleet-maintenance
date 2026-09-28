import {
  AlertTriangle, ArchiveRestore, Bell, Boxes, CalendarDays, Check, CheckCircle2, ChevronRight,
  ClipboardCheck, Cloud, CloudOff, Database, FileDown, FileUp, Gauge, History, Home, LogOut,
  Menu, Package, Play, Plus, RefreshCw, Search, Settings, ShieldCheck, ShoppingCart, Truck,
  Users, Warehouse, Wrench, X,
} from "lucide-react";
import { createContext, FormEvent, ReactNode, useCallback, useContext, useEffect, useId, useRef, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { abortActiveRequests, api, ApiError, AUTHORIZATION_FAILURE_EVENT, formatDate, identifier, isRecord, Json, query, records, sessionStatus, text, uploadAttachment } from "./api";
import { PartIdentifierLookup } from "./PartIdentifierLookup";
import {
  cacheGet, cachePut, cancelSync, clearOfflineData, discardOperation, getOfflineIdentity,
  invalidateOfflineAccess, onSync, outboxItems, queueOperation, refreshSyncLabel,
  reauthorizeOfflineWork, resumeSync, setOfflineIdentity, startForegroundSync, syncNow, SyncLabel,
} from "./offline";

interface User {
  id: string;
  username: string;
  name: string;
  organization_id: string;
  roles: string[];
  permissions: string[];
  navigation: { label: string; href: string }[];
}

interface Bootstrap {
  user: User;
  offline_expires_at: string;
  offline_grant: string;
  assets: Json[];
  work_orders: Json[];
  parts: Json[];
  stock: Json[];
  inspection_templates: Json[];
  sync_conflicts: Json[];
  locations: Json[];
}

interface AppData {
  bootstrap: Bootstrap;
  refresh: () => Promise<void>;
  can: (permission: string) => boolean;
}

function offlineBootstrapSnapshot(fresh: Bootstrap): Bootstrap {
  const permits = (permission: string) => fresh.user.permissions.includes("*") || fresh.user.permissions.includes(permission);
  const workOrders = permits("maintenance.execute")
    ? fresh.work_orders.filter((row) => isWorkAssignedTo(row, fresh.user))
    : [];
  const workAssetIds = new Set(workOrders.map((row) => String(row.asset_id ?? "")));
  const driver = fresh.user.roles.includes("driver");
  const inventoryFieldAccess = permits("inventory.issue") || permits("inventory.return");
  return {
    ...fresh,
    assets: driver ? fresh.assets : fresh.assets.filter((row) => workAssetIds.has(identifier(row))),
    work_orders: workOrders,
    parts: inventoryFieldAccess ? fresh.parts : [],
    stock: inventoryFieldAccess ? fresh.stock : [],
    inspection_templates: permits("inspections.create") ? fresh.inspection_templates : [],
    sync_conflicts: [],
    locations: [],
  };
}

const AppContext = createContext<AppData | null>(null);
function useApp(): AppData {
  const context = useContext(AppContext);
  if (!context) throw new Error("Application data is unavailable");
  return context;
}

const navIcons: Record<string, typeof Home> = {
  Home, Today: Home, Overview: Home, Work: Wrench, "My Work": ClipboardCheck, Assets: Truck,
  Schedule: CalendarDays, Parts: Package, Inventory: Boxes, "Purchase Orders": ShoppingCart,
  Vendors: Warehouse, Alerts: Bell, Reports: Gauge, Inspect: ClipboardCheck, Inspections: ClipboardCheck,
  "Report Problem": AlertTriangle, "My Reports": History, Administration: Users, Audit: ShieldCheck,
  System: Settings, Devices: Cloud, "Data quality": Database, "Integration health": Cloud,
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong";
}

function value(form: FormData, name: string): string {
  return String(form.get(name) ?? "").trim();
}

function optional(valueToCheck: string): string | undefined {
  return valueToCheck || undefined;
}

function scalar(row: Json, key: string): string {
  const item = row[key];
  return typeof item === "string" || typeof item === "number" ? String(item) : "";
}

function physicalLocation(row: Json): string {
  const binCode = scalar(row, "bin_code");
  if (binCode) return binCode;
  const warehouseCode = scalar(row, "warehouse_code");
  const code = scalar(row, "code");
  return warehouseCode && code ? `${warehouseCode}/${code}` : warehouseCode || code || "—";
}

function physicalLocationLabel(row: Json): string {
  const location = physicalLocation(row);
  const description = scalar(row, "name");
  return description && description !== location ? `${location} · ${description}` : location;
}

function workAssignees(row: Json): Json[] {
  return Array.isArray(row.assignees) ? row.assignees.filter(isRecord) : [];
}

function isWorkAssignedTo(row: Json, user: User): boolean {
  const assignments = workAssignees(row);
  return assignments.length
    ? assignments.some((assignment) => String(assignment.user_id ?? "") === user.id)
    : String(row.assigned_to_id ?? "") === user.id;
}

function workAssigneeNames(row: Json): string {
  const names = workAssignees(row).map((assignment) => text(assignment, "display_name", "name")).filter(Boolean);
  return names.length ? names.join(", ") : text(row, "assigned_to_name", "assigned_to") || "Unassigned";
}

interface AssignmentCandidate {
  key: string;
  label: string;
  userId?: string;
  sourceSystem?: string;
  externalEmployeeId?: string;
}

function assignmentKey(row: Json): string {
  const userId = scalar(row, "user_id");
  return userId ? `user:${userId}` : `external:${scalar(row, "source_system")}:${scalar(row, "external_employee_id")}`;
}

function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState("");

  const load = useCallback(async () => {
    let cachedBootstrap: Bootstrap | undefined;
    try { cachedBootstrap = await cacheGet<Bootstrap>("bootstrap"); }
    catch { /* IndexedDB availability must not block an online sign-in. */ }
    try {
      if (navigator.onLine && !(await sessionStatus())) {
        await invalidateOfflineAccess();
        setBootstrap(null);
        setAuthError("");
        return;
      }
      const fresh = await api<Bootstrap>("/api/v1/bootstrap/");
      if (!navigator.onLine && new Date(fresh.offline_expires_at).valueOf() <= Date.now()) {
        await invalidateOfflineAccess();
        throw new ApiError("Offline access has expired. Connect to the network and sign in again.", 401, "offline_access_expired");
      }
      const previousIdentity = getOfflineIdentity();
      const freshIdentity = { user_id: fresh.user.id, organization_id: fresh.user.organization_id, expires_at: fresh.offline_expires_at, offline_grant: fresh.offline_grant };
      if (previousIdentity && (previousIdentity.user_id !== freshIdentity.user_id || previousIdentity.organization_id !== freshIdentity.organization_id)) {
        await invalidateOfflineAccess();
      }
      setOfflineIdentity(freshIdentity);
      await reauthorizeOfflineWork(freshIdentity);
      await cachePut("bootstrap", offlineBootstrapSnapshot(fresh));
      setBootstrap(fresh);
      setAuthError("");
    } catch (error) {
      const networkUnavailable = !navigator.onLine || error instanceof ApiError && error.status === 0;
      const cached = networkUnavailable ? cachedBootstrap : undefined;
      const usableOffline = cached && new Date(cached.offline_expires_at).valueOf() > Date.now();
      if (usableOffline) setBootstrap(cached);
      else {
        setBootstrap(null);
        if (!(error instanceof ApiError && error.status === 401 && error.code !== "offline_access_expired")) setAuthError(errorMessage(error));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const revoke = () => {
      void invalidateOfflineAccess();
      setBootstrap(null);
      setAuthError("Your session is no longer authorized. Sign in again to recover saved work for this account.");
    };
    window.addEventListener(AUTHORIZATION_FAILURE_EVENT, revoke);
    return () => window.removeEventListener(AUTHORIZATION_FAILURE_EVENT, revoke);
  }, []);

  if (loading) return <div className="splash" role="status"><span className="brand">Fleetline</span><span>Loading your workspace…</span></div>;
  if (!bootstrap) return <Login onSignedIn={load} initialError={authError} />;

  const appData: AppData = {
    bootstrap,
    refresh: load,
    can: (permission) => bootstrap.user.permissions.includes("*") || bootstrap.user.permissions.includes(permission),
  };
  return <AppContext.Provider value={appData}><Shell user={bootstrap.user} onSignOut={() => setBootstrap(null)} /></AppContext.Provider>;
}

function Login({ onSignedIn, initialError }: { onSignedIn: () => Promise<void>; initialError: string }) {
  const [otpRequired, setOtpRequired] = useState(false);
  const [error, setError] = useState(initialError);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const result = await api<Json>("/api/v1/auth/login/", {
        method: "POST",
        json: { username: value(form, "username"), password: value(form, "password"), otp: value(form, "otp") },
      });
      const signedInUser = isRecord(result.user) ? result.user : null;
      const previousIdentity = getOfflineIdentity();
      if (signedInUser && previousIdentity && (
        identifier(signedInUser) !== previousIdentity.user_id
        || String(signedInUser.organization_id ?? "") !== previousIdentity.organization_id
      )) await invalidateOfflineAccess();
      await onSignedIn();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "mfa_required") setOtpRequired(true);
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return <main className="login-page">
    <section className="login-panel" aria-labelledby="sign-in-heading">
      <div className="brand brand-dark">Fleetline</div>
      <h1 id="sign-in-heading">Sign in</h1>
      <p>Fleet maintenance and parts management</p>
      {error && <Notice tone="danger">{error}</Notice>}
      <form onSubmit={submit}>
        <Field label="Username" name="username" autoComplete="username" required autoFocus />
        <Field label="Password" name="password" type="password" autoComplete="current-password" required />
        {otpRequired && <Field label="One-time code" name="otp" inputMode="numeric" autoComplete="one-time-code" required autoFocus hint="Enter the six-digit code from your authenticator." />}
        <button className="button primary wide" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </section>
  </main>;
}

function Shell({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobileViewport, setMobileViewport] = useState(() => window.matchMedia("(max-width: 700px)").matches);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [signOutError, setSignOutError] = useState("");
  const [sync, setSync] = useState<{ label: SyncLabel; count: number; message: string }>({ label: "Synchronized", count: 0, message: "" });
  const notifications = useCollection("/api/v1/notifications/", "notifications");
  const navigate = useNavigate();
  const location = useLocation();
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeMenuButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const stopEvents = onSync(setSync);
    const stopSync = startForegroundSync();
    return () => { stopEvents(); stopSync(); };
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 700px)");
    const update = () => setMobileViewport(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!mobileViewport || !menuOpen) return;
    const focusTimer = window.setTimeout(() => closeMenuButton.current?.focus(), 0);
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButton.current?.focus();
      }
    };
    document.addEventListener("keydown", escape);
    return () => { window.clearTimeout(focusTimer); document.removeEventListener("keydown", escape); };
  }, [menuOpen, mobileViewport]);

  useEffect(() => {
    const timer = window.setTimeout(() => document.getElementById("main-content")?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [location.pathname]);

  function closeMenu() {
    setMenuOpen(false);
    if (mobileViewport) menuButton.current?.focus();
  }

  async function signOut() {
    await cancelSync();
    const pending = await outboxItems();
    if (pending.length && !window.confirm("Unsynchronized work remains on this device. Sign out and remove it?")) {
      resumeSync();
      void syncNow();
      return;
    }
    try {
      abortActiveRequests();
      const result = await api<{ ok?: boolean }>("/api/v1/auth/logout/", { method: "POST", json: {} });
      if (result.ok !== true) throw new Error("The server did not confirm sign-out.");
      await clearOfflineData();
      setSignOutError("");
      onSignOut();
      navigate("/");
    } catch (caught) {
      resumeSync();
      void syncNow();
      setSignOutError(errorMessage(caught));
    }
  }

  async function markNotificationRead(notification: Json) {
    await api("/api/v1/notifications/", { method: "POST", json: { id: identifier(notification) } });
    await notifications.reload();
  }

  const unreadCount = notifications.rows.filter((notification) => !notification.read_at).length;
  const notificationCenter = (mobile = false) => <div className={`notification-center ${mobile ? "mobile-notifications" : ""}`}>
    <button type="button" className={mobile ? "icon-button inverse" : "icon-button"} aria-label={`Notifications${unreadCount ? `, ${unreadCount} unread` : ""}`} aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen((open) => !open)}>
      <Bell aria-hidden="true" />{unreadCount > 0 && <span className="notification-count" aria-hidden="true">{unreadCount}</span>}
    </button>
    {notificationsOpen && <section className="notification-menu" aria-label="Notifications">
      <header><h2>Notifications</h2><button className="icon-button" type="button" aria-label="Close notifications" onClick={() => setNotificationsOpen(false)}><X /></button></header>
      <RecordList rows={notifications.rows} loading={notifications.loading} empty="No notifications." render={(notification) => {
        const resourceType = text(notification, "resource_type"); const resourceId = text(notification, "resource_id");
        const target = /work.?order/i.test(resourceType) && resourceId !== "—" ? `/work-orders/${resourceId}` : /asset/i.test(resourceType) && resourceId !== "—" ? `/assets/${resourceId}` : /defect/i.test(resourceType) ? "/defects" : "";
        return <article className={notification.read_at ? "notification read" : "notification unread"}><div><strong>{text(notification, "title")}</strong><p>{text(notification, "body")}</p><small>{notification.read_at ? "Read" : "Unread"}{resourceType !== "—" ? ` · ${titleCase(resourceType)} ${resourceId}` : ""}</small></div><div className="notification-actions">{target && <Link className="button secondary" to={target} onClick={() => setNotificationsOpen(false)}>View</Link>}{!notification.read_at && <button type="button" className="button secondary" onClick={() => void markNotificationRead(notification)}>Mark as read</button>}</div></article>;
      }} />
    </section>}
  </div>;

  const mobileNav = user.navigation.slice(0, 4);
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="mobile-header"><Link className="brand" to="/">Fleetline</Link><div className="mobile-header-actions">{notificationCenter(true)}<button ref={menuButton} className="icon-button inverse" aria-label="Open navigation" aria-expanded={menuOpen} aria-controls="primary-navigation" onClick={() => setMenuOpen(!menuOpen)}><Menu /></button></div></header>
    <aside id="primary-navigation" className={menuOpen ? "sidebar open" : "sidebar"} aria-label="Primary navigation" aria-hidden={mobileViewport ? !menuOpen : undefined} inert={mobileViewport && !menuOpen ? true : undefined}>
      <div className="sidebar-heading"><Link className="brand" to="/">Fleetline</Link><button ref={closeMenuButton} className="icon-button inverse mobile-only" aria-label="Close navigation" onClick={closeMenu}><X /></button></div>
      <nav>{user.navigation.map((item) => <NavigationLink key={`${item.label}-${item.href}`} item={item} close={closeMenu} />)}</nav>
      <button className="sign-out" onClick={() => void signOut()}><LogOut /> Sign out</button>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <GlobalSearch />
        <SyncStatus {...sync} />
        {notificationCenter()}
        <div className="profile"><span className="avatar">{user.name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase()}</span><span><strong>{user.name}</strong><small>{user.roles.map(titleCase).join(", ")}</small></span></div>
      </header>
      <div className="mobile-sync"><SyncStatus {...sync} /></div>
      <main id="main-content" className="content" tabIndex={-1}>
        {signOutError && <Notice tone="danger">Sign-out failed: {signOutError}</Notice>}
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/report-problem" element={<ReportDefect />} />
          <Route path="/inspections" element={<Inspections />} />
          <Route path="/defects" element={<DefectsAndRequests key="defects" />} />
          <Route path="/requests" element={<DefectsAndRequests key="requests" initialTab="requests" />} />
          <Route path="/my-work" element={<WorkOrders mine />} />
          <Route path="/work-orders" element={<WorkOrders />} />
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
          <Route path="/assets" element={<Assets />} />
          <Route
            path="/assets/external/:sourceSystem/:externalId"
            element={<ExternalAssetResolver />}
          />
          <Route path="/assets/:id" element={<AssetDetail />} />
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/parts" element={<Parts />} />
          <Route path="/inventory" element={<Inventory />} />
          <Route path="/purchase-orders" element={<Purchasing key="orders" />} />
          <Route path="/vendors" element={<Purchasing key="vendors" initialTab="vendors" />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/data-quality" element={<DataQuality />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/components/:id" element={<ComponentDetail />} />
          <Route path="/administration" element={<Administration />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
    <nav className="bottom-nav" aria-label="Mobile navigation">{mobileNav.map((item) => <NavigationLink key={`mobile-${item.label}`} item={item} close={() => undefined} />)}</nav>
  </div>;
}

function NavigationLink({ item, close }: { item: { label: string; href: string }; close: () => void }) {
  const Icon = navIcons[item.label] ?? ChevronRight;
  return <NavLink to={item.href} onClick={close} className={({ isActive }) => isActive ? "nav-link active" : "nav-link"}><Icon aria-hidden="true" /><span>{item.label}</span></NavLink>;
}

function SyncStatus({ label, count, message }: { label: SyncLabel; count: number; message: string }) {
  const Icon = label === "Synchronized" ? CheckCircle2 : label === "Sync conflict" ? AlertTriangle : navigator.onLine ? RefreshCw : CloudOff;
  return <button type="button" className={`sync-status ${label === "Sync conflict" ? "danger" : ""}`} onClick={() => void syncNow()} data-testid="sync-status" title={message || "Synchronize saved field work"}>
    <Icon aria-hidden="true" /><span aria-live="polite" aria-atomic="true"><strong>{label}</strong><small data-testid="outbox-count">{label === "Sync conflict" ? `Needs your attention · ${count} change${count === 1 ? "" : "s"}` : count ? `${count} change${count === 1 ? "" : "s"} pending` : navigator.onLine ? "All changes are up to date" : "Working offline"}</small></span>
  </button>;
}

function GlobalSearch() {
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  return <form className="global-search" role="search" onSubmit={(event) => { event.preventDefault(); if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`); }}>
    <Search aria-hidden="true" />
    <label className="sr-only" htmlFor="global-search">Search assets, work orders, parts, vendors, and components</label>
    <input id="global-search" value={q} onChange={(event) => setQ(event.target.value)} placeholder="Search assets, work orders, parts, vendors…" />
  </form>;
}

function Dashboard() {
  const { bootstrap, can } = useApp();
  const [report, setReport] = useState<Json | null>(null);
  useEffect(() => {
    if (can("reports.shop") || can("reports.all") || can("reports.executive") || can("reports.inventory") || can("reports.purchasing") || can("reports.integration")) {
      void api<Json>("/api/v1/reports/operations/").then(setReport).catch(() => undefined);
    }
  }, [can]);
  const summary = isRecord(report?.summary) ? report.summary : {};
  const roles = bootstrap.user.roles;
  if (roles.includes("parts_clerk")) return <Navigate to="/parts" replace />;
  if (roles.includes("purchasing_manager")) return <Navigate to="/purchase-orders" replace />;
  if (roles.includes("system_admin")) return <Page title="System"><div className="split"><Panel title="Administration"><Link className="list-row" to="/administration"><Users /><span><strong>Users and webhooks</strong><small>Manage access, revocation, and outbound integrations.</small></span><ChevronRight /></Link></Panel><Panel title="Governance"><Link className="list-row" to="/audit"><ShieldCheck /><span><strong>Audit history</strong><small>Review append-only administrative and security events.</small></span><ChevronRight /></Link></Panel></div></Page>;
  if (roles.includes("integration_admin")) return <Page title="Integration health"><div className="split"><Panel title="Devices"><Link className="list-row" to="/integrations"><Cloud /><span><strong>Device registry</strong><small>Register devices, manage time-bounded assignments, and submit fixtures.</small></span><ChevronRight /></Link></Panel><Panel title="Data quality"><Link className="list-row" to="/data-quality"><Database /><span><strong>Review quarantined readings</strong><small>Maintenance remains available while telemetry needs attention.</small></span><ChevronRight /></Link></Panel></div></Page>;
  if (roles.includes("driver")) return <Page title="Home" actions={<Link className="button primary" to="/report-problem"><Plus /> Report a defect</Link>}><Panel title="Assigned assets"><RecordList rows={bootstrap.assets} empty="No assets are assigned to you." render={(row) => <Link className="list-row" to={`/assets/${identifier(row)}`}><Truck /><span><strong>{text(row, "unit_number")}</strong><small>{text(row, "status")}</small></span><ChevronRight /></Link>} /></Panel><Panel title="Field actions"><div className="quick-actions"><Link className="button secondary" to="/inspections"><ClipboardCheck /> Start inspection</Link><Link className="button secondary" to="/defects"><History /> My reports</Link></div></Panel></Page>;
  const openWork = bootstrap.work_orders.filter((row) => !["Closed", "Cancelled"].includes(text(row, "status")));
  const oos = bootstrap.assets.filter((row) => /out.?of.?service/i.test(text(row, "status"))).length;
  const overdue = Number(summary.pm_overdue ?? 0);
  const blocked = openWork.filter((row) => /parts|blocked/i.test(text(row, "status"))).length;
  if (roles.includes("management")) return <Page title="Overview"><section className="metrics" aria-label="Operational summary"><Metric icon={<Truck />} label="Out of service" value={String(summary.out_of_service ?? 0)} /><Metric icon={<Wrench />} label="Open work orders" value={String(summary.open_work_orders ?? 0)} /><Metric icon={<CalendarDays />} label="PM overdue" value={String(summary.pm_overdue ?? 0)} /></section><Panel title="Management report"><Link className="list-row" to="/reports"><Gauge /><span><strong>Open operational report</strong><small>Trace availability, downtime, and cost results to source records.</small></span><ChevronRight /></Link></Panel></Page>;
  return <Page title={roles.includes("technician") ? "My work" : "Today"} actions={<Link className="button primary" to={roles.includes("technician") ? "/my-work" : "/work-orders"}><Wrench /> View work</Link>}>
    <section className="metrics" aria-label="Operational summary">
      <Metric icon={<Truck />} label="Out of service" value={String(summary.out_of_service ?? oos)} tone="safety" />
      <Metric icon={<ClipboardCheck />} label="PM overdue" value={String(overdue)} tone="safety" />
      <Metric icon={<Package />} label="Blocked by parts" value={String(blocked)} tone="safety" />
    </section>
    <div className="split">
      <Panel title={bootstrap.user.roles.includes("technician") ? "Ready for you" : "Needs decision"}>
        <RecordList rows={openWork.slice(0, 5)} empty="No open work needs attention." render={(row) => <Link to={`/work-orders/${identifier(row)}`} className="list-row"><Wrench /><span><strong>{text(row, "number")} · {text(row, "asset_unit_number", "asset")}</strong><small>{text(row, "summary", "status")}</small></span><ChevronRight /></Link>} />
      </Panel>
      <Panel title="Upcoming PM"><RecordList rows={bootstrap.assets.filter((row) => /due|overdue/i.test(text(row, "pm_status", "due_status"))).slice(0, 5)} empty="No preventive maintenance is currently due." render={(row) => <Link to={`/assets/${identifier(row)}`} className="list-row"><CalendarDays /><span><strong>{text(row, "unit_number")}</strong><small>{text(row, "pm_status", "due_status")}</small></span><ChevronRight /></Link>} /></Panel>
    </div>
    <Panel title="Work orders" action={<Link className="button secondary" to="/work-orders">View all work orders</Link>}><WorkTable rows={openWork.slice(0, 8)} /></Panel>
    <SyncIssues serverRows={bootstrap.sync_conflicts} showEmpty={can("integrations.manage")} />
  </Page>;
}

function SyncIssues({ serverRows, showEmpty }: { serverRows: Json[]; showEmpty: boolean }) {
  const [localRows, setLocalRows] = useState<Json[]>([]);
  const [error, setError] = useState("");
  const loadLocal = useCallback(async () => {
    const items = await outboxItems();
    setLocalRows(items
      .filter((item) => item.status === "conflict" || item.status === "rejected")
      .map((item) => ({
        id: item.operation_id,
        operation_id: item.operation_id,
        operation_type: item.type,
        message: item.message,
        client_payload: item.client_payload ?? { type: item.type, payload: item.payload, created_at: item.created_at },
        server_payload: item.server_payload ?? {},
        resolution_options: item.resolution_options ?? ["review_server", "discard_local"],
        local: true,
      })));
  }, []);
  useEffect(() => {
    const load = () => void loadLocal();
    const timer = window.setTimeout(load, 0);
    const stop = onSync(load);
    return () => { window.clearTimeout(timer); stop(); };
  }, [loadLocal]);
  const rowsById = new Map<string, Json>();
  serverRows.forEach((row) => rowsById.set(String(row.operation_id ?? identifier(row)), { ...row, id: row.operation_id ?? identifier(row) }));
  localRows.forEach((row) => rowsById.set(identifier(row), { ...(rowsById.get(identifier(row)) ?? {}), ...row }));
  const rows = [...rowsById.values()];
  if (!showEmpty && !rows.length) return null;
  return <Panel title="Data quality">
    {error && <Notice tone="danger">{error}</Notice>}
    <RecordList rows={rows} empty="No synchronization conflicts." render={(row) => {
      const client = isRecord(row.client_payload) ? row.client_payload : isRecord(row.client) ? row.client : {};
      const server = isRecord(row.server_payload) ? row.server_payload : isRecord(row.server) ? row.server : {};
      const payload = isRecord(client.payload) ? client.payload : client;
      const serverWork = isRecord(server.work_order) ? server.work_order : {};
      const workOrderId = String(payload.work_order_id ?? serverWork.id ?? "");
      return <article className="list-row danger"><AlertTriangle /><div><strong>Sync conflict · {text(row, "operation_type", "type")}</strong><small>{text(row, "message")}</small><details><summary>Compare saved and server values</summary><h4>Saved on this device</h4><pre>{JSON.stringify(client, null, 2)}</pre><h4>Current server value</h4><pre>{JSON.stringify(server, null, 2)}</pre></details><div className="row-actions">{workOrderId && <Link className="button secondary" to={`/work-orders/${workOrderId}`}>Review current work</Link>}{row.local === true && <button type="button" className="button safety" onClick={() => void (async () => {
        if (!window.confirm("Discard this saved change and its attachments from this device? This cannot be undone.")) return;
        try { await discardOperation(identifier(row)); setError(""); await loadLocal(); }
        catch (caught) { setError(errorMessage(caught)); }
      })()}>Discard saved change</button>}</div></div></article>;
    }} />
  </Panel>;
}

function ReportDefect() {
  const { bootstrap } = useApp();
  const [area, setArea] = useState("Other");
  const [unsafe, setUnsafe] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const areas = ["Brakes", "Tires", "Lights", "Engine", "Other"];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    const files = Array.from((form.elements.namedItem("attachment") as HTMLInputElement).files ?? []);
    try {
      await queueOperation("defect.create", {
        asset_id: value(data, "asset_id"), category: area, severity: unsafe ? "safety" : "medium",
        safety_related: unsafe, unsafe_to_operate: unsafe, description: value(data, "description"), reported_at: new Date().toISOString(),
      }, files);
      setNotice(navigator.onLine ? "Saved on this device. Synchronizing now." : "Saved on this device. It will synchronize when a connection is available.");
      form.reset();
      setArea("Other");
      setUnsafe(false);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  return <Page title="Report a defect" narrow>
    {notice && <Notice tone="success">{notice}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    <form className="field-form" onSubmit={(event) => void submit(event)}>
      <SelectField label="Asset" name="asset_id" required options={bootstrap.assets.map((row) => ({ value: identifier(row), label: `${text(row, "unit_number")} · ${text(row, "status")}` }))} />
      <fieldset><legend>What area?</legend><div className="choice-grid">{areas.map((item) => <button type="button" key={item} className={area === item ? "choice selected" : "choice"} aria-pressed={area === item} onClick={() => setArea(item)}><AreaIcon area={item} />{item}</button>)}</div></fieldset>
      <fieldset><legend>Is it unsafe to drive?</legend><div className="choice-grid two"><button type="button" className={unsafe ? "choice selected safety" : "choice safety"} aria-pressed={unsafe} onClick={() => setUnsafe(true)}><AlertTriangle />Yes</button><button type="button" className={!unsafe ? "choice selected" : "choice"} aria-pressed={!unsafe} onClick={() => setUnsafe(false)}><ShieldCheck />No</button></div></fieldset>
      <Field label="Describe it" name="description" as="textarea" maxLength={500} placeholder="Provide details about the issue…" required />
      <Field label="Add photo or file" name="attachment" type="file" accept="image/jpeg,image/png,image/webp,application/pdf,text/plain" />
      <button className="button primary wide"><ClipboardCheck /> Submit defect</button>
    </form>
  </Page>;
}

function AreaIcon({ area }: { area: string }) {
  if (area === "Engine") return <Settings />;
  if (area === "Lights") return <Bell />;
  if (area === "Tires") return <Gauge />;
  if (area === "Brakes") return <ArchiveRestore />;
  return <Wrench />;
}

function Inspections() {
  const { bootstrap, can } = useApp();
  const inspections = useCollection("/api/v1/maintenance/inspections/", "inspections");
  const [templateId, setTemplateId] = useState("");
  const [replacesId, setReplacesId] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const template = bootstrap.inspection_templates.find((row) => identifier(row) === templateId);
  const templateItems = template && Array.isArray(template.questions) ? template.questions.filter(isRecord) : [];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const responses = templateItems.map((item, index) => ({
      question_id: identifier(item), result: value(data, `item_${index}`) || "pass", notes: value(data, `note_${index}`),
    }));
    const files = Array.from((form.elements.namedItem("attachment") as HTMLInputElement).files ?? []);
    try {
      const payload = {
        asset_id: value(data, "asset_id"), template_id: value(data, "template_id"), meter_value: optional(value(data, "meter_value")),
        responses, acknowledgment: value(data, "acknowledgment") === "yes" ? "I confirm this inspection is complete and accurate." : "", notes: value(data, "notes"),
        submitted_at: new Date().toISOString(), replaces_id: optional(replacesId),
      };
      if (replacesId) {
        if (!navigator.onLine) throw new Error("Connect to replace a voided inspection so the server can verify its history.");
        const created = await api<Json>("/api/v1/maintenance/inspections/", { method: "POST", json: payload });
        const inspection = isRecord(created.inspection) ? created.inspection : created;
        await Promise.all(files.map((file) => uploadAttachment(file, "Inspection", identifier(inspection), crypto.randomUUID())));
        await inspections.reload();
      } else await queueOperation("inspection.submit", payload, files);
      setNotice("Saved on this device. Inspection will synchronize without losing your answers or attachment.");
      setError("");
      form.reset();
      setTemplateId("");
      setReplacesId("");
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function voidInspection(row: Json) {
    const reason = window.prompt("Reason for voiding and replacing this inspection");
    if (!reason) return;
    try { await api(`/api/v1/maintenance/inspections/${identifier(row)}/void/`, { method: "POST", json: { reason } }); setReplacesId(identifier(row)); setNotice("Original inspection was voided and remains in history. Complete the form to submit its replacement."); await inspections.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  return <Page title="Inspections" narrow actions={<Link className="button secondary" to="/defects"><History /> View submitted reports</Link>}>
    {notice && <Notice tone="success">{notice}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    <form className="field-form" onSubmit={(event) => void submit(event)}>
      <SelectField label="Asset" name="asset_id" required options={bootstrap.assets.map((row) => ({ value: identifier(row), label: text(row, "unit_number") }))} />
      <SelectField label="Inspection template" name="template_id" required value={templateId} onChange={setTemplateId} options={bootstrap.inspection_templates.map((row) => ({ value: identifier(row), label: text(row, "name", "title") }))} />
      <Field label="Current meter" name="meter_value" type="number" min="0" step="0.1" inputMode="decimal" />
      {templateItems.map((item, index) => <fieldset className="inspection-item" key={identifier(item) || index}><legend>{text(item, "label", "name", "question")}</legend><div className="inline-options"><label><input type="radio" name={`item_${index}`} value="pass" defaultChecked /> Pass</label><label><input type="radio" name={`item_${index}`} value="fail" /> Needs attention</label><label><input type="radio" name={`item_${index}`} value="na" /> Not applicable</label></div><Field label="Item note" hideLabel name={`note_${index}`} placeholder="Optional note" /></fieldset>)}
      <Field label="Inspection notes" name="notes" as="textarea" maxLength={2000} />
      <Field label="Add photo or file" name="attachment" type="file" multiple accept="image/jpeg,image/png,image/webp,application/pdf" />
      <label className="acknowledgment"><input type="checkbox" name="acknowledgment" value="yes" required /> I confirm this inspection is complete and accurate.</label>
      <button className="button primary wide"><Check /> {replacesId ? "Submit replacement inspection" : "Submit inspection"}</button>
    </form>
    <Panel title="Inspection history"><RecordList rows={inspections.rows} loading={inspections.loading} empty="No submitted inspections found." render={(row) => <div className="workflow-row"><div><Status value={text(row, "status")} /><strong>{text(row, "asset", "asset_unit_number")} · {text(row, "template_name", "template")}</strong><small>{formatDate(row.submitted_at ?? row.created_at)}</small></div>{can("maintenance.manage") && text(row, "status") === "Submitted" && <button className="button secondary" onClick={() => void voidInspection(row)}>Void and replace</button>}</div>} /></Panel>
  </Page>;
}

function DefectsAndRequests({ initialTab = "defects" }: { initialTab?: "defects" | "requests" }) {
  const { bootstrap, can } = useApp();
  const canViewRequests = can("maintenance.manage") || can("maintenance.execute") || can("work_orders.view");
  const [tab, setTab] = useState(initialTab === "requests" && canViewRequests ? initialTab : "defects");
  const defects = useCollection("/api/v1/maintenance/defects/", "defects");
  const requests = useCollection(canViewRequests ? "/api/v1/maintenance/requests/" : "", "requests");
  const technicians = useCollection(can("maintenance.manage") ? "/api/v1/users/?role=technician" : "", "users");
  const [requestForWork, setRequestForWork] = useState<Json | null>(null);
  const [defectForCorrection, setDefectForCorrection] = useState<Json | null>(null);
  const [error, setError] = useState("");

  async function act(path: string, label: string, body: Json = {}) {
    try {
      if (label === "Approve request") {
        await api(path, { method: "POST", json: { status: "Triaged" } });
        await api(path, { method: "POST", json: { status: "Approved" } });
      } else await api(path, { method: "POST", json: body });
      await Promise.all([defects.reload(), requests.reload()]);
      setError("");
    } catch (caught) { setError(`${label}: ${errorMessage(caught)}`); }
  }

  async function createWorkOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!requestForWork) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const created = await api<Json>(`/api/v1/maintenance/requests/${identifier(requestForWork)}/work-order/`, { method: "POST", json: { assigned_to_id: value(data, "assigned_to_id"), summary: value(data, "summary"), priority: value(data, "priority") } });
      const work = isRecord(created.work_order) ? created.work_order : null;
      if (work) await api(`/api/v1/maintenance/work-orders/${identifier(work)}/transition/`, { method: "POST", json: { status: "Ready" } });
      setRequestForWork(null);
      await requests.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function correctDefect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!defectForCorrection) return;
    const form = event.currentTarget; const data = new FormData(form);
    const details = value(data, "repair_details");
    const files = Array.from((form.elements.namedItem("evidence") as HTMLInputElement).files ?? []);
    try {
      const evidence = await Promise.all(files.map(async (file) => {
        const uploaded = await uploadAttachment(file, "Defect", identifier(defectForCorrection), crypto.randomUUID());
        return identifier(uploaded.attachment);
      }));
      await api(`/api/v1/maintenance/defects/${identifier(defectForCorrection)}/transition/`, { method: "POST", json: { status: "Corrected", repair_details: details, reason: details, evidence_attachment_ids: evidence } });
      setDefectForCorrection(null); setError(""); form.reset(); await defects.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  function canCorrectDefect(defect: Json): boolean {
    const sourceRequest = requests.rows.find((request) => text(request, "defect_id") === identifier(defect));
    return Boolean(sourceRequest && bootstrap.work_orders.some((workOrder) => text(workOrder, "request_id") === identifier(sourceRequest)
      && isWorkAssignedTo(workOrder, bootstrap.user) && !/closed|cancelled/i.test(text(workOrder, "status"))));
  }

  return <Page title={can("maintenance.manage") ? "Triage defects" : "My reports"} actions={can("defects.create") ? <Link className="button primary" to="/report-problem"><Plus /> Report a defect</Link> : undefined}>
    <Tabs value={tab} onChange={(next) => setTab(next as typeof tab)} tabs={[{ id: "defects", label: "Defects" }, ...(canViewRequests ? [{ id: "requests", label: "Maintenance requests" }] : [])]}>
      {error && <Notice tone="danger">{error}</Notice>}
      {tab === "defects" ? <Panel title="Defects"><RecordList rows={defects.rows} loading={defects.loading} empty="No defects found." render={(row) => <article className="workflow-row"><div><Status value={text(row, "status")} /><h3>{text(row, "asset_unit_number", "asset")} · {text(row, "area", "category")}</h3><p>{text(row, "description")}</p><small>{formatDate(row.reported_at ?? row.created_at)}</small></div><div className="row-actions">{can("maintenance.manage") && <>{!/^Acknowledged$/i.test(text(row, "status")) && <button className="button secondary" onClick={() => void act(`/api/v1/maintenance/defects/${identifier(row)}/transition/`, "Acknowledge", { status: "Acknowledged", to_status: "Acknowledged" })}>Acknowledge</button>}<button className="button primary" onClick={() => void act(`/api/v1/maintenance/defects/${identifier(row)}/request/`, "Create maintenance request")}>Create maintenance request</button></>}{bootstrap.user.roles.includes("technician") && text(row, "status") === "InRepair" && canCorrectDefect(row) && <button className="button primary" onClick={() => setDefectForCorrection(row)}>Record defect correction</button>}</div></article>} /></Panel>
        : <Panel title="Maintenance requests"><RecordList rows={requests.rows} loading={requests.loading} empty="No maintenance requests found." render={(row) => <article className="workflow-row"><div><Status value={text(row, "status")} /><h3>{text(row, "number")} · {text(row, "asset_unit_number", "asset")}</h3><p>{text(row, "summary", "description")}</p></div>{can("maintenance.manage") && <div className="row-actions">{!/^Approved$/i.test(text(row, "status")) && <button className="button secondary" onClick={() => void act(`/api/v1/maintenance/requests/${identifier(row)}/transition/`, "Approve request", { status: "Approved", to_status: "Approved" })}>Approve request</button>}{/^Approved$/i.test(text(row, "status")) && <button className="button primary" onClick={() => setRequestForWork(row)}>Create work order</button>}</div>}</article>} /></Panel>}
      {requestForWork && <Panel title="Create work order" action={<button className="icon-button" aria-label="Cancel work-order creation" onClick={() => setRequestForWork(null)}><X /></button>}><form className="form-grid" onSubmit={(event) => void createWorkOrder(event)}><Field label="Summary" name="summary" defaultValue={text(requestForWork, "summary", "description")} required /><SelectField label="Assigned technician" name="assigned_to_id" required options={technicians.rows.map((user) => ({ value: identifier(user), label: `${text(user, "name")} · ${text(user, "username")}` }))} /><SelectField label="Priority" name="priority" required options={[{ value: "normal", label: "Normal" }, { value: "high", label: "High" }, { value: "safety", label: "Safety" }]} /><button className="button primary">Create work order</button></form></Panel>}
      {defectForCorrection && <Panel title="Record defect correction" action={<button className="icon-button" aria-label="Cancel defect correction" onClick={() => setDefectForCorrection(null)}><X /></button>}><form className="form-grid" onSubmit={(event) => void correctDefect(event)}><Field label="Repair details" name="repair_details" as="textarea" required /><Field label="Repair evidence" name="evidence" type="file" multiple accept="image/jpeg,image/png,image/webp,application/pdf" /><button className="button primary">Save defect correction</button></form></Panel>}
    </Tabs>
  </Page>;
}

function WorkOrders({ mine = false }: { mine?: boolean }) {
  const { bootstrap } = useApp();
  const collection = useCollection("/api/v1/maintenance/work-orders/", "work_orders");
  const rows = mine ? collection.rows.filter((row) => isWorkAssignedTo(row, bootstrap.user)) : collection.rows;
  return <Page title={mine ? "My work" : "Work orders"}><Panel title={mine ? "Assigned work" : "All work orders"}><WorkTable rows={rows} loading={collection.loading} /></Panel></Page>;
}

function WorkTable({ rows, loading = false }: { rows: Json[]; loading?: boolean }) {
  if (loading) return <Loading />;
  if (!rows.length) return <Empty>No work orders found.</Empty>;
  return <div className="table-scroll"><table><thead><tr><th>Work order</th><th>Asset</th><th>Priority</th><th>Status</th><th>Assigned</th><th>Due</th></tr></thead><tbody>{rows.map((row) => <tr key={identifier(row)}><td><Link to={`/work-orders/${identifier(row)}`}>{text(row, "number")}</Link></td><td>{text(row, "asset_unit_number", "asset")}</td><td><Status value={text(row, "priority")} /></td><td><Status value={text(row, "status")} /></td><td>{workAssigneeNames(row)}</td><td>{formatDate(row.target_date ?? row.due_at ?? row.due_date)}</td></tr>)}</tbody></table></div>;
}

function WorkOrderDetail() {
  const { id = "" } = useParams();
  const { bootstrap, can, refresh } = useApp();
  const detail = useResource(`/api/v1/maintenance/work-orders/${id}/`);
  const technicians = useCollection(can("maintenance.manage") ? "/api/v1/users/?role=technician" : "", "users");
  const externalEmployees = useCollection(can("maintenance.manage") ? "/api/v1/maintenance/personnel/external/" : "", "employees");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const timerKey = `fleetline:work-timer:${bootstrap.user.id}:${id}`;
  const [timerStartedAt, setTimerStartedAt] = useState(() => localStorage.getItem(timerKey) ?? "");
  const [timerClock, setTimerClock] = useState(() => Date.now());
  const row = isRecord(detail.data?.work_order) ? detail.data.work_order : detail.data;
  const assetMeters = useCollection(row?.asset_id ? `/api/v1/assets/${String(row.asset_id)}/meters/` : "", "meters");
  const woComponents = useCollection(row?.asset_id ? `/api/v1/assets/${String(row.asset_id)}/components/` : "", "installations");
  const tasks = Array.isArray(row?.tasks) ? row.tasks.filter(isRecord) : records(detail.data, "tasks");
  const installedHere = woComponents.rows.filter((installation) => !installation.removed_at);

  async function installFromWorkOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    if (!row?.asset_id) return;
    try { await api(`/api/v1/assets/${String(row.asset_id)}/components/`, { method: "POST", json: { kind: value(data, "kind"), serial_number: value(data, "serial_number"), manufacturer: value(data, "manufacturer"), model: value(data, "model"), work_order_id: id } }); form.reset(); setError(""); await woComponents.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function removeFromWorkOrder(componentId: string) {
    const reason = window.prompt("Reason for removal");
    if (!reason) return;
    try { await api(`/api/v1/assets/components/${componentId}/remove/`, { method: "POST", json: { reason, work_order_id: id } }); setError(""); await woComponents.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  const stockTransactions = Array.isArray(row?.stock_transactions) ? row.stock_transactions.filter(isRecord) : [];
  const completionReadings = assetMeters.rows.flatMap((meter) => isRecord(meter.current_reading) ? [{ ...meter.current_reading, meter_name: text(meter, "name"), meter_unit: text(meter, "unit") }] : []);
  const assignmentCandidates: AssignmentCandidate[] = [
    ...technicians.rows.map((user) => ({ key: `user:${identifier(user)}`, label: `${text(user, "name")} · ${text(user, "username")}`, userId: identifier(user) })),
    ...externalEmployees.rows.map((employee) => ({ key: assignmentKey(employee), label: `${text(employee, "display_name")} · Gator Hub${text(employee, "job_title") ? ` · ${text(employee, "job_title")}` : ""}`, sourceSystem: text(employee, "source_system"), externalEmployeeId: text(employee, "external_employee_id") })),
  ];
  const knownAssignmentKeys = new Set(assignmentCandidates.map((candidate) => candidate.key));
  if (row) {
    workAssignees(row).forEach((assignment) => {
      const key = assignmentKey(assignment);
      if (!knownAssignmentKeys.has(key)) {
        assignmentCandidates.push({
          key,
          label: `${text(assignment, "display_name")} · unavailable upstream`,
          userId: scalar(assignment, "user_id") || undefined,
          sourceSystem: scalar(assignment, "source_system") || undefined,
          externalEmployeeId: scalar(assignment, "external_employee_id") || undefined,
        });
      }
    });
  }
  const currentAssignmentKeys = row
    ? workAssignees(row).length
      ? workAssignees(row).map(assignmentKey)
      : scalar(row, "assigned_to_id") ? [`user:${scalar(row, "assigned_to_id")}`] : []
    : [];
  const currentLead = row ? workAssignees(row).find((assignment) => text(assignment, "role") === "lead") : undefined;
  const currentLeadKey = currentLead ? assignmentKey(currentLead) : currentAssignmentKeys[0] ?? "";

  useEffect(() => {
    if (!timerStartedAt) return;
    const interval = window.setInterval(() => setTimerClock(Date.now()), 30_000);
    return () => window.clearInterval(interval);
  }, [timerStartedAt]);

  async function transition(label: string, status: string) {
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/transition/`, { method: "POST", json: { status, to_status: status, reason: `${label} from Fleetline` } });
      await Promise.all([detail.reload(), refresh()]);
      setError("");
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function completeTask(task: Json) {
    await queueOperation("task.complete", { work_order_id: id, task_id: identifier(task), completed: true, base_version: row?.version });
    await refreshSyncLabel();
  }

  async function addTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/tasks/`, {
        method: "POST",
        json: {
          title: value(data, "title"),
          instructions: value(data, "instructions"),
          required: value(data, "required") === "yes",
          sequence: Number(value(data, "sequence")),
          ...(value(data, "component_id") ? { component_id: value(data, "component_id") } : {}),
        },
      });
      form.reset();
      setNotice("Work-order task added.");
      setError("");
      await detail.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function completeWork(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const summary = value(formData, "completion_summary");
    const completionMeterId = value(formData, "completion_meter_id");
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/`, { method: "PATCH", json: { completion_summary: summary, base_version: row?.version } });
      const target = row?.requires_qc ? "QC" : "Completed";
      await api(`/api/v1/maintenance/work-orders/${id}/transition/`, { method: "POST", json: { status: target, completion_summary: summary, completion_meter_id: optional(completionMeterId) } });
      await Promise.all([detail.reload(), refresh()]);
      setError("");
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function verifyAndClose() {
    try {
      if (!row) throw new Error("Work order not found");
      if (text(row, "status") === "QC") {
        await api(`/api/v1/maintenance/work-orders/${id}/transition/`, { method: "POST", json: { status: "Completed", completion_summary: text(row, "completion_summary") } });
      }
      if (row.request_id) {
        const requestPayload = await api<Json>("/api/v1/maintenance/requests/");
        const sourceRequest = records(requestPayload, "maintenance_requests", "requests").find((item) => identifier(item) === String(row.request_id));
        if (sourceRequest?.defect_id) {
          const defectPayload = await api<Json>("/api/v1/maintenance/defects/");
          const sourceDefect = records(defectPayload, "defects").find((item) => identifier(item) === String(sourceRequest.defect_id));
          const transitions: Record<string, string[]> = {
            Open: ["Acknowledged", "InRepair", "Corrected", "Verified", "Closed"],
            Acknowledged: ["InRepair", "Corrected", "Verified", "Closed"],
            Deferred: ["Acknowledged", "InRepair", "Corrected", "Verified", "Closed"],
            InRepair: ["Corrected", "Verified", "Closed"], Corrected: ["Verified", "Closed"], Verified: ["Closed"], Closed: [],
          };
          for (const target of transitions[text(sourceDefect, "status")] ?? []) {
            await api(`/api/v1/maintenance/defects/${String(sourceRequest.defect_id)}/transition/`, { method: "POST", json: { status: target, reason: "Repair verified during work-order closure" } });
          }
        }
        if (sourceRequest && text(sourceRequest, "status") !== "Closed") {
          await api(`/api/v1/maintenance/requests/${identifier(sourceRequest)}/transition/`, { method: "POST", json: { status: "Closed", reason: "Repair completed and verified" } });
        }
      }
      await api(`/api/v1/maintenance/work-orders/${id}/transition/`, { method: "POST", json: { status: "Closed" } });
      await Promise.all([detail.reload(), refresh()]);
      setNotice("Work verified and closed. Linked request and defect history were advanced to closure.");
      setError("");
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function reopen() {
    const reason = window.prompt("Reason for reopening this closed work order");
    if (!reason) return;
    try { await api(`/api/v1/maintenance/work-orders/${id}/transition/`, { method: "POST", json: { status: "Reopened", reason } }); await Promise.all([detail.reload(), refresh()]); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function addLabor(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/labor/`, { method: "POST", json: { minutes: Math.round(Number(value(data, "hours")) * 60), note: value(data, "description") } });
      form.reset();
      await detail.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  function startLaborTimer() {
    const startedAt = new Date().toISOString();
    localStorage.setItem(timerKey, startedAt);
    setTimerStartedAt(startedAt);
    setTimerClock(Date.now());
    setNotice("Labor timer started. It will remain available after a reload.");
  }

  async function stopLaborTimer() {
    if (!timerStartedAt) return;
    const endedAt = new Date();
    const minutes = Math.max(1, Math.round((endedAt.valueOf() - new Date(timerStartedAt).valueOf()) / 60_000));
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/labor/`, { method: "POST", json: { started_at: timerStartedAt, ended_at: endedAt.toISOString(), minutes, note: "Timed labor" } });
      localStorage.removeItem(timerKey); setTimerStartedAt(""); setNotice(`${minutes} minute${minutes === 1 ? "" : "s"} of labor saved.`); setError(""); await detail.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function addNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const body = value(data, "note");
    await queueOperation("work_note.create", { work_order_id: id, body, note: body, expected_updated_at: row?.updated_at });
    form.reset();
  }

  async function assignTeam(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!row) return;
    const form = new FormData(event.currentTarget);
    const selectedKeys = new Set(form.getAll("assignee_key").map(String));
    const selected = assignmentCandidates.filter((candidate) => selectedKeys.has(candidate.key));
    const leadAssignmentKey = value(form, "team_lead");
    if (!selected.length) {
      setError("Choose at least one assigned person.");
      return;
    }
    if (leadAssignmentKey && !selected.some((candidate) => candidate.key === leadAssignmentKey)) {
      setError("The team lead must also be assigned to this work order.");
      return;
    }
    try {
      await api(`/api/v1/maintenance/work-orders/${id}/assignments/`, {
        method: "POST",
        json: {
          base_version: Number(row.version),
          assignees: selected.map((candidate) => candidate.userId
            ? { user_id: candidate.userId, role: candidate.key === leadAssignmentKey ? "lead" : "technician" }
            : { source_system: candidate.sourceSystem, external_employee_id: candidate.externalEmployeeId, role: candidate.key === leadAssignmentKey ? "lead" : "technician" }),
          reason: value(form, "assignment_reason"),
        },
      });
      setNotice("Assigned work-order team saved.");
      setError("");
      await Promise.all([detail.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (detail.loading) return <Loading />;
  if (detail.error || !row) return <Page title="Work order"><Notice tone="danger">{detail.error || "Work order not found"}</Notice></Page>;
  const status = text(row, "status");
  return <Page title={`${text(row, "number")} · ${text(row, "asset_unit_number", "asset")}`} subtitle={text(row, "summary")} actions={<Status value={status} />}>
    {notice && <Notice tone="success">{notice}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    <div className="command-bar">
      {can("maintenance.manage") && status === "Draft" && <button className="button primary" onClick={() => void transition("Ready for work", "Ready")}><Check /> Ready for work</button>}
      {can("maintenance.execute") && /approved|ready|assigned|open/i.test(status) && <button className="button primary" onClick={() => void transition("Start work", "InProgress")}><Play /> Start work</button>}
      {can("maintenance.manage") && /completed|qc|quality/i.test(status.replace(/\s/g, "")) && <button className="button primary" onClick={() => void verifyAndClose()}><ShieldCheck /> Verify and close</button>}
      {can("maintenance.manage") && status === "Closed" && <button className="button secondary" onClick={() => void reopen()}><ArchiveRestore /> Reopen work order</button>}
    </div>
    {can("maintenance.execute") && /progress|started/i.test(status) && <Panel title="Complete work"><form className="form-grid" onSubmit={(event) => void completeWork(event)}><Field label="Completion summary" name="completion_summary" required /><SelectField label="Completion meter" name="completion_meter_id" options={completionReadings.map((reading) => ({ value: identifier(reading), label: `${text(reading, "meter_name")} · ${text(reading, "value")} ${text(reading, "meter_unit")}` }))} /><button className="button primary"><Check /> Complete work</button></form></Panel>}
    <div className="split">
      <Panel title="Tasks"><RecordList rows={tasks} empty="No tasks are assigned." render={(task) => <div className="task-row"><span className={/completed/i.test(text(task, "status")) ? "check checked" : "check"}>{/completed/i.test(text(task, "status")) ? <><Check aria-hidden="true" /><span className="sr-only">Completed</span></> : null}</span><span><strong>{text(task, "title", "name", "description")}</strong><small>{text(task, "instructions")}{/completed/i.test(text(task, "status")) ? " · Completed" : ""}{isRecord(task.component) ? ` · Component: ${text(task.component, "kind_label")} · ${text(task.component, "serial_number")}` : ""}</small></span>{!/completed/i.test(text(task, "status")) && can("maintenance.execute") && <button className="button secondary" onClick={() => void completeTask(task)}>Complete task</button>}</div>} /></Panel>
      <Panel title="Work details"><dl className="details"><dt>Priority</dt><dd>{text(row, "priority")}</dd><dt>Assigned team</dt><dd>{workAssigneeNames(row)}</dd><dt>Due</dt><dd>{formatDate(row.due_at ?? row.due_date)}</dd><dt>Complaint</dt><dd>{text(row, "complaint", "description", "summary")}</dd></dl></Panel>
    </div>
    {(can("maintenance.execute") || can("maintenance.manage")) && <Panel title={`Components on ${text(row, "asset_unit_number", "asset")}`} action={!/completed|closed|cancelled/i.test(status) ? <details className="action-details"><summary className="button secondary"><Plus /> Install</summary><form className="popover-form" onSubmit={(event) => void installFromWorkOrder(event)}><SelectField label="Kind" name="kind" required options={[{ value: "engine", label: "Engine" }, { value: "transmission", label: "Transmission" }, { value: "reefer_unit", label: "Reefer unit" }, { value: "apu", label: "APU" }, { value: "axle", label: "Axle" }, { value: "aftertreatment", label: "Aftertreatment" }, { value: "other", label: "Other" }]} /><Field label="Component serial number" name="serial_number" required /><Field label="Make" name="manufacturer" /><Field label="Model" name="model" /><button className="button primary">Install</button></form></details> : undefined}>
      <RecordList rows={installedHere} loading={woComponents.loading} empty="No components recorded on this truck." render={(installation) => <div className="list-row"><Wrench /><span><strong>{text(isRecord(installation.component) ? installation.component : {}, "kind_label")}</strong><small><Link to={`/components/${text(installation, "component_id")}`}>{text(isRecord(installation.component) ? installation.component : {}, "serial_number")}</Link> · installed {formatDate(installation.installed_at)}</small></span>{!/completed|closed|cancelled/i.test(status) && <button className="button secondary" onClick={() => void removeFromWorkOrder(text(installation, "component_id"))}>Remove</button>}</div>} />
    </Panel>}
    {can("maintenance.manage") && !/completed|closed|cancelled/i.test(status) && <Panel title="Add work-order task"><form className="form-grid" onSubmit={(event) => void addTask(event)}><Field label="Task title" name="title" required /><Field label="Task instructions" name="instructions" as="textarea" /><Field label="Sequence" name="sequence" type="number" min="1" step="1" defaultValue={String(tasks.length + 1)} required /><label className="acknowledgment"><input type="checkbox" name="required" value="yes" defaultChecked /> Required task</label>{installedHere.length > 0 && <SelectField label="Component (optional)" name="component_id" options={[{ value: "", label: "No specific component" }, ...installedHere.map((installation) => ({ value: text(installation, "component_id"), label: `${text(isRecord(installation.component) ? installation.component : {}, "kind_label")} · ${text(isRecord(installation.component) ? installation.component : {}, "serial_number")}` }))]} />}<button className="button primary">Add task</button></form></Panel>}
    {can("maintenance.manage") && !/completed|closed|cancelled/i.test(status) && <Panel title="Assignment"><form key={scalar(row, "version")} className="form-grid" aria-label="Work-order assignment" onSubmit={(event) => void assignTeam(event)}><fieldset className="inspection-item"><legend>Assigned people</legend>{assignmentCandidates.length ? assignmentCandidates.map((candidate) => <label className="acknowledgment" key={candidate.key}><input type="checkbox" name="assignee_key" value={candidate.key} defaultChecked={currentAssignmentKeys.includes(candidate.key)} /> {candidate.label}</label>) : <p>No active technicians or synchronized personnel are available.</p>}</fieldset><label className="field"><span className="field-label">Team lead</span><select name="team_lead" defaultValue={currentLeadKey}><option value="">No designated team lead</option>{assignmentCandidates.map((candidate) => <option key={candidate.key} value={candidate.key}>{candidate.label}</option>)}</select><small>Optional; a team lead must also be checked above.</small></label><Field label="Assignment note" name="assignment_reason" hint="Optional reason retained in the assignment history." maxLength={500} /><button className="button secondary">Save assigned team</button></form></Panel>}
    {can("maintenance.execute") && <><div className="split"><Panel title="Labor timer"><div className="timer-control" role="status" aria-live="polite">{timerStartedAt ? <><div><strong>Timer running</strong><small>Started {formatDate(timerStartedAt)} · {formatElapsed(new Date(timerStartedAt).valueOf(), timerClock)}</small></div><button type="button" className="button primary" onClick={() => void stopLaborTimer()}>Stop and save labor</button></> : <><div><strong>No active timer</strong><small>Start when hands-on work begins.</small></div><button type="button" className="button primary" onClick={startLaborTimer}>Start labor timer</button></>}</div></Panel><Panel title="Add labor manually"><form className="compact-form" onSubmit={(event) => void addLabor(event)}><Field label="Hours" name="hours" type="number" min="0.01" step="0.01" required /><Field label="Work performed" name="description" required /><button className="button primary">Add labor</button></form></Panel></div><Panel title="Work notes"><form className="compact-form" onSubmit={(event) => void addNote(event)}><Field label="Note" name="note" as="textarea" required /><button className="button secondary">Save work note</button></form></Panel></>}
    <Panel title="Work-order parts history" action={can("inventory.issue") || can("inventory.transact") ? <Link className="button secondary" to={`/inventory?work_order_id=${id}`}><Package /> Reserve or issue parts</Link> : undefined}>{can("financial.view") && <div className="cost-summary"><span>Part cost</span><strong>${text(row, "part_cost")}</strong></div>}<div className="table-scroll"><table><thead><tr><th>Posted</th><th>Transaction</th><th>Part</th><th>Bin</th><th>Quantity</th>{can("financial.view") && <th>Cost</th>}</tr></thead><tbody>{stockTransactions.map((transaction) => <tr key={identifier(transaction)}><td>{formatDate(transaction.created_at)}</td><td>{titleCase(text(transaction, "type"))}</td><td>{text(transaction, "part_number")}</td><td>{text(transaction, "bin_code")}</td><td>{text(transaction, "quantity")}</td>{can("financial.view") && <td>${text(transaction, "total_cost")}</td>}</tr>)}</tbody></table>{!stockTransactions.length && <Empty>No stock transactions are linked to this work order.</Empty>}</div></Panel>
  </Page>;
}


function ComponentDetail() {
  const { id = "" } = useParams();
  const detail = useResource(`/api/v1/assets/components/${id}/`);
  const component = isRecord(detail.data?.component) ? detail.data.component : {};
  const installations = records(detail.data, "installations");
  const tasks = records(detail.data, "tasks");
  const workOrders = records(detail.data, "work_orders");
  const meterAt = (installation: Json, key: string) => { const list = Array.isArray(installation[key]) ? installation[key].filter(isRecord) : []; return list.length ? `${text(list[0], "value")} ${text(list[0], "unit")}` : "—"; };
  if (detail.loading) return <Loading />;
  if (!detail.data) return <Page title="Component"><Notice tone="danger">{detail.error || "Component not found"}</Notice></Page>;
  const installedOn = isRecord(component.installed_on) ? component.installed_on : null;
  return <Page title={`${text(component, "kind_label")} ${text(component, "serial_number")}`} subtitle={[text(component, "manufacturer"), text(component, "model")].filter((part) => part !== "—").join(" ")} actions={<Status value={installedOn ? "Installed" : "Not installed"} />}>
    <Panel title="Installation history">
      <div className="table-scroll"><table><thead><tr><th>Truck</th><th>Installed</th><th>Meter at install</th><th>Removed</th><th>Meter at removal</th><th>Reason</th><th>Work order</th></tr></thead><tbody>{installations.map((installation) => <tr key={identifier(installation)}><td><Link to={`/assets/${text(installation, "asset_id")}`}>{text(isRecord(installation.asset) ? installation.asset : {}, "unit_number")}</Link></td><td>{formatDate(installation.installed_at)}</td><td>{meterAt(installation, "installed_meters")}</td><td>{installation.removed_at ? formatDate(installation.removed_at) : "—"}</td><td>{meterAt(installation, "removed_meters")}</td><td>{text(installation, "removal_reason")}</td><td>{text(installation, "installed_work_order_number")}{text(installation, "removed_work_order_number") !== "—" ? ` / ${text(installation, "removed_work_order_number")}` : ""}</td></tr>)}</tbody></table></div>
    </Panel>
    <Panel title="Service history">
      <RecordList rows={tasks} empty="No work-order tasks reference this component." render={(task) => <Link className="list-row" to={`/work-orders/${text(task, "work_order_id")}`}><ClipboardCheck /><span><strong>{text(task, "title")}</strong><small>{text(task, "work_order_number")} · {text(task, "unit_number")} · {text(task, "status")}</small></span><ChevronRight /></Link>} />
      {workOrders.length > 0 && <details><summary>Install and removal work orders ({workOrders.length})</summary><RecordList rows={workOrders} empty="" render={(workOrder) => <Link className="list-row" to={`/work-orders/${identifier(workOrder)}`}><Wrench /><span><strong>{text(workOrder, "number")}</strong><small>{text(workOrder, "summary")}</small></span><ChevronRight /></Link>} /></details>}
    </Panel>
  </Page>;
}


function ExternalAssetResolver() {
  const { sourceSystem = "", externalId = "" } = useParams();
  const [target, setTarget] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void api<Json>(`/api/v1/assets/external/${encodeURIComponent(sourceSystem)}/${encodeURIComponent(externalId)}/`)
      .then((payload) => {
        const asset = isRecord(payload.asset) ? payload.asset : null;
        if (!asset || typeof asset.id !== "string") throw new Error("Asset lookup returned no record");
        if (active) setTarget(`/assets/${asset.id}`);
      })
      .catch((caught) => {
        if (active) setError(`Could not open this linked asset: ${errorMessage(caught)}`);
      });
    return () => { active = false; };
  }, [sourceSystem, externalId]);

  if (target) return <Navigate to={target} replace />;
  return <Page title="Opening linked asset" narrow>
    {error ? <Notice tone="danger">{error}</Notice> : <Loading />}
  </Page>;
}


function Assets() {
  const { can, refresh } = useApp();
  const collection = useCollection("/api/v1/assets/", "assets");
  const [error, setError] = useState("");

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api("/api/v1/assets/", { method: "POST", json: {
        unit_number: value(data, "unit_number"), vin: value(data, "vin"), asset_type: value(data, "asset_type"),
        make: value(data, "make"), model: value(data, "model"), status: "Available",
        specs: { equipment: { engine: { type: value(data, "engine_type") } } },
      } });
      form.reset();
      await Promise.all([collection.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  return <Page title="Assets" actions={can("assets.manage") ? <details className="action-details"><summary className="button primary"><Plus /> New asset</summary><form className="popover-form" onSubmit={(event) => void create(event)}><Field label="Unit number" name="unit_number" required /><Field label="VIN" name="vin" maxLength={17} /><Field label="Make" name="make" /><Field label="Model" name="model" /><Field label="Truck type" name="asset_type" defaultValue="Truck" required /><fieldset><legend>Engine information</legend><Field label="Engine type" name="engine_type" /></fieldset><button className="button primary">Create asset</button></form></details> : undefined}>
    {error && <Notice tone="danger">{error}</Notice>}
    <Panel title="Fleet"><div className="table-scroll"><table><thead><tr><th>Unit</th><th>Make / model</th><th>Truck type</th><th>Status</th><th>Current meter</th></tr></thead><tbody>{collection.rows.map((row) => <tr key={identifier(row)}><td><Link to={`/assets/${identifier(row)}`}>{text(row, "unit_number")}</Link></td><td>{[text(row, "make"), text(row, "model")].filter((item) => item !== "—").join(" ") || "—"}</td><td>{text(row, "asset_type_name", "asset_type")}</td><td><Status value={text(row, "status")} /></td><td>{text(row, "current_meter", "odometer", "meter_value")}</td></tr>)}</tbody></table>{collection.loading ? <Loading /> : !collection.rows.length && <Empty>No assets found.</Empty>}</div></Panel>
  </Page>;
}

function AssetDetail() {
  const { id = "" } = useParams();
  const { can, refresh } = useApp();
  const detail = useResource(`/api/v1/assets/${id}/`);
  const history = useCollection(`/api/v1/assets/${id}/history/`, "timeline", "status_events", "events", "history");
  const meters = useCollection(`/api/v1/assets/${id}/meters/`, "readings", "meter_readings");
  const components = useCollection(`/api/v1/assets/${id}/components/`, "installations");
  const [selectedMeter, setSelectedMeter] = useState("");
  const [meterType, setMeterType] = useState("");
  const [meterUnit, setMeterUnit] = useState("");
  const [retirementOpen, setRetirementOpen] = useState(false);
  const [error, setError] = useState("");
  const row = isRecord(detail.data?.asset) ? detail.data.asset : detail.data;
  const readings: Json[] = meters.rows.flatMap((meterRow): Json[] => Array.isArray(meterRow.readings) ? meterRow.readings.filter(isRecord).map((reading): Json => ({ ...reading, meter_name: text(meterRow, "name", "kind") })) : []);
  const retirementMeters = meters.rows.filter((meterRow) => meterRow.active !== false && /odometer|engine_hours/i.test(text(meterRow, "kind")));

  async function meter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api(`/api/v1/assets/${id}/meters/`, { method: "POST", json: {
        meter_id: optional(selectedMeter),
        kind: meterType, name: meterType === "odometer" ? "Odometer" : "Engine hours",
        value: value(data, "meter_value"), unit: meterUnit, observed_at: new Date().toISOString(),
      } });
      form.reset(); setSelectedMeter(""); setMeterType(""); setMeterUnit("");
      await Promise.all([meters.reload(), detail.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function installComponent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const body: Record<string, string> = { kind: value(data, "kind"), serial_number: value(data, "serial_number"), manufacturer: value(data, "manufacturer"), model: value(data, "model") };
    if (value(data, "installed_at")) body.installed_at = new Date(value(data, "installed_at")).toISOString();
    if (value(data, "work_order_id")) body.work_order_id = value(data, "work_order_id");
    try { await api(`/api/v1/assets/${id}/components/`, { method: "POST", json: body }); form.reset(); setError(""); await Promise.all([components.reload(), history.reload()]); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function removeComponent(componentId: string) {
    const reason = window.prompt("Reason for removal");
    if (!reason) return;
    try { await api(`/api/v1/assets/components/${componentId}/remove/`, { method: "POST", json: { reason } }); setError(""); await Promise.all([components.reload(), history.reload()]); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function availability(status: string) {
    const prompt = status === "Available" ? "Reason for returning this asset to service" : "Reason this asset is out of service";
    const reason = window.prompt(prompt);
    if (!reason) return;
    try {
      await api(`/api/v1/assets/${id}/availability/`, { method: "POST", json: { status, reason } });
      await Promise.all([detail.reload(), history.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function retireAsset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const finalReadings = retirementMeters.map((meterRow) => isRecord(meterRow.current_reading) ? identifier(meterRow.current_reading) : "");
    if (finalReadings.some((readingId) => !readingId)) { setError("Record an accepted final reading for every active odometer and engine-hours meter before retirement."); return; }
    try {
      await api(`/api/v1/assets/${id}/availability/`, { method: "POST", json: { status: "Retired", reason: value(data, "reason"), disposition: value(data, "disposition"), final_meter_reading_ids: finalReadings } });
      setRetirementOpen(false); setError(""); await Promise.all([detail.reload(), history.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function correctMeter(reading: Json) {
    const correctedValue = window.prompt("Corrected meter value", text(reading, "value"));
    if (!correctedValue) return;
    const reason = window.prompt("Reason for this correction");
    if (!reason) return;
    try { await api(`/api/v1/assets/meter-readings/${identifier(reading)}/correct/`, { method: "POST", json: { value: correctedValue, reason } }); await Promise.all([meters.reload(), history.reload()]); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function saveEquipment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!row) return;
    const data = new FormData(event.currentTarget);
    const currentSpecs = isRecord(row.specs) ? row.specs : {};
    const currentEquipment = isRecord(currentSpecs.equipment) ? currentSpecs.equipment : {};
    const section = (name: string): Json => isRecord(currentEquipment[name]) ? currentEquipment[name] : {};
    try {
      await api(`/api/v1/assets/${id}/`, { method: "PATCH", json: {
        specs: {
          ...currentSpecs,
          equipment: {
            ...currentEquipment,
            engine: { ...section("engine"), manufacturer: value(data, "engine_manufacturer"), model: value(data, "engine_model"), type: value(data, "engine_type") },
            transmission: { ...section("transmission"), manufacturer: value(data, "transmission_manufacturer"), model: value(data, "transmission_model") },
            axle: { ...section("axle"), configuration: value(data, "axle_configuration"), ratio: value(data, "axle_ratio") },
            emissions: { ...section("emissions"), manufacturer: value(data, "emissions_manufacturer"), model: value(data, "emissions_model") },
            auxiliary: { ...section("auxiliary"), body_or_upfit: value(data, "body_or_upfit"), pto_or_hydraulic: value(data, "pto_or_hydraulic") },
          },
        },
      } });
      setError("");
      await detail.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (detail.loading) return <Loading />;
  if (!row) return <Page title="Asset"><Notice tone="danger">{detail.error || "Asset not found"}</Notice></Page>;
  const isOos = /out.?of.?service/i.test(text(row, "status"));
  const isRetired = text(row, "status") === "Retired";
  const specs = isRecord(row.specs) ? row.specs : {};
  const equipment = isRecord(specs.equipment) ? specs.equipment : {};
  const engine = isRecord(equipment.engine) ? equipment.engine : {};
  const transmission = isRecord(equipment.transmission) ? equipment.transmission : {};
  const axle = isRecord(equipment.axle) ? equipment.axle : {};
  const emissions = isRecord(equipment.emissions) ? equipment.emissions : {};
  const auxiliary = isRecord(equipment.auxiliary) ? equipment.auxiliary : {};
  const openComponents = components.rows.filter((installation) => !installation.removed_at);
  const pastComponents = components.rows.filter((installation) => installation.removed_at);
  const installedEngine = openComponents.find((installation) => text(isRecord(installation.component) ? installation.component : {}, "kind") === "engine");
  const meterAt = (installation: Json, key: string) => { const list = Array.isArray(installation[key]) ? installation[key].filter(isRecord) : []; return list.length ? `${text(list[0], "value")} ${text(list[0], "unit")}` : "—"; };
  const componentLabel = (installation: Json) => { const component = isRecord(installation.component) ? installation.component : {}; return [text(component, "kind_label"), [text(component, "manufacturer"), text(component, "model")].filter((part) => part !== "—").join(" ")].filter((part) => part && part !== "—").join(" · "); };
  const componentSerial = (installation: Json) => text(isRecord(installation.component) ? installation.component : {}, "serial_number");
  return <Page title={text(row, "unit_number")} subtitle={text(row, "vin")} actions={<Status value={text(row, "status")} />}>
    {error && <Notice tone="danger">{error}</Notice>}
    {!isRetired && (can("assets.status") || can("assets.manage")) && <div className="command-bar">{can("assets.status") && (isOos ? <button className="button primary" onClick={() => void availability("Available")}><ShieldCheck /> Return to service</button> : <button className="button safety" onClick={() => void availability("OutOfService")}><AlertTriangle /> Place out of service</button>)}{can("assets.manage") && <button className="button secondary" onClick={() => setRetirementOpen((open) => !open)} aria-expanded={retirementOpen}><ArchiveRestore /> Retire asset</button>}</div>}
    {retirementOpen && can("assets.manage") && !isRetired && <Panel title="Retire asset"><Notice tone="info">Retirement is permanent operational history. Confirm disposition and the accepted final reading for every active meter.</Notice><form className="form-grid" onSubmit={(event) => void retireAsset(event)}><Field label="Retirement reason" name="reason" required /><Field label="Disposition" name="disposition" required hint="For example: sold, scrapped, transferred, or retained for parts." /><fieldset className="inspection-item"><legend>Final meter readings</legend>{retirementMeters.length ? retirementMeters.map((meterRow) => { const reading = isRecord(meterRow.current_reading) ? meterRow.current_reading : undefined; return <p key={identifier(meterRow)}><strong>{text(meterRow, "name")}</strong>: {text(reading, "value")} {text(meterRow, "unit")} · reading ID <code>{reading ? identifier(reading) : "Missing"}</code></p>; }) : <p>No active odometer or engine-hours meters require a final reading.</p>}</fieldset><div className="row-actions"><button className="button safety">Confirm asset retirement</button><button type="button" className="button secondary" onClick={() => setRetirementOpen(false)}>Cancel retirement</button></div></form></Panel>}
    <Panel title="Components" action={can("assets.manage") || can("maintenance.manage") ? <details className="action-details"><summary className="button secondary"><Plus /> Install component</summary><form className="popover-form" onSubmit={(event) => void installComponent(event)}><SelectField label="Kind" name="kind" required options={[{ value: "engine", label: "Engine" }, { value: "transmission", label: "Transmission" }, { value: "reefer_unit", label: "Reefer unit" }, { value: "apu", label: "APU" }, { value: "axle", label: "Axle" }, { value: "aftertreatment", label: "Aftertreatment" }, { value: "other", label: "Other" }]} /><Field label="Component serial number" name="serial_number" required /><Field label="Make" name="manufacturer" /><Field label="Model" name="model" /><details><summary>Advanced</summary><Field label="Installed at" name="installed_at" type="datetime-local" hint="Leave blank for now." /><Field label="Work order ID" name="work_order_id" hint="Optional. Technicians install from the work-order screen instead." /></details><button className="button primary">Install</button></form></details> : undefined}>
      {openComponents.length === 0 ? <p className="empty">No components recorded on this truck.</p> : <details open><summary>{openComponents.length === 1 ? "1 installed component" : `${openComponents.length} installed components`}</summary><div className="table-scroll"><table><thead><tr><th>Component</th><th>Serial</th><th>Installed</th><th>Meter at install</th>{!isRetired && (can("assets.manage") || can("maintenance.manage")) && <th><span className="sr-only">Actions</span></th>}</tr></thead><tbody>{openComponents.map((installation) => <tr key={identifier(installation)}><td>{componentLabel(installation)}</td><td><Link to={`/components/${text(installation, "component_id")}`}>{componentSerial(installation)}</Link></td><td>{formatDate(installation.installed_at)}</td><td>{meterAt(installation, "installed_meters")}</td>{!isRetired && (can("assets.manage") || can("maintenance.manage")) && <td><button className="button secondary" onClick={() => void removeComponent(text(installation, "component_id"))}>Remove</button></td>}</tr>)}</tbody></table></div></details>}
      {pastComponents.length > 0 && <details><summary>Past components ({pastComponents.length})</summary><div className="table-scroll"><table><thead><tr><th>Component</th><th>Serial</th><th>Installed</th><th>Removed</th><th>Meter at removal</th><th>Reason</th></tr></thead><tbody>{pastComponents.map((installation) => <tr key={identifier(installation)}><td>{componentLabel(installation)}</td><td><Link to={`/components/${text(installation, "component_id")}`}>{componentSerial(installation)}</Link></td><td>{formatDate(installation.installed_at)}</td><td>{formatDate(installation.removed_at)}</td><td>{meterAt(installation, "removed_meters")}</td><td>{text(installation, "removal_reason")}</td></tr>)}</tbody></table></div></details>}
    </Panel>
    <div className="split"><Panel title="Asset details"><dl className="details"><dt>Truck number</dt><dd>{text(row, "unit_number")}</dd><dt>VIN</dt><dd>{text(row, "vin")}</dd><dt>Make</dt><dd>{text(row, "make")}</dd><dt>Model</dt><dd>{text(row, "model")}</dd><dt>Truck type</dt><dd>{text(row, "asset_type_name", "asset_type")}</dd></dl></Panel><Panel title="Engine information"><dl className="details"><dt>Engine manufacturer</dt><dd>{text(engine, "manufacturer")}</dd><dt>Engine model</dt><dd>{text(engine, "model")}</dd><dt>Engine type</dt><dd>{text(engine, "type")}</dd><dt>Engine serial number</dt><dd>{installedEngine ? <Link to={`/components/${text(installedEngine, "component_id")}`}>{componentSerial(installedEngine)}</Link> : "—"}</dd></dl></Panel>
      <Panel title="Equipment details"><dl className="details"><dt>Transmission</dt><dd>{[text(transmission, "manufacturer"), text(transmission, "model")].filter((item) => item !== "—").join(" ") || "—"}</dd><dt>Axle configuration</dt><dd>{text(axle, "configuration")}</dd><dt>Axle ratio</dt><dd>{text(axle, "ratio")}</dd><dt>Emissions system</dt><dd>{[text(emissions, "manufacturer"), text(emissions, "model")].filter((item) => item !== "—").join(" ") || "—"}</dd><dt>Body / upfit</dt><dd>{text(auxiliary, "body_or_upfit")}</dd><dt>PTO / hydraulic</dt><dd>{text(auxiliary, "pto_or_hydraulic")}</dd></dl>{can("assets.manage") && <details className="action-details"><summary className="button secondary">Edit equipment details</summary><form className="popover-form" onSubmit={(event) => void saveEquipment(event)}><fieldset><legend>Engine</legend><Field label="Manufacturer" name="engine_manufacturer" defaultValue={scalar(engine, "manufacturer")} /><Field label="Model" name="engine_model" defaultValue={scalar(engine, "model")} /><Field label="Type" name="engine_type" defaultValue={scalar(engine, "type")} /></fieldset><fieldset><legend>Drivetrain</legend><Field label="Transmission manufacturer" name="transmission_manufacturer" defaultValue={scalar(transmission, "manufacturer")} /><Field label="Transmission model" name="transmission_model" defaultValue={scalar(transmission, "model")} /><Field label="Axle configuration" name="axle_configuration" defaultValue={scalar(axle, "configuration")} hint="For example: 6x4 tandem." /><Field label="Axle ratio" name="axle_ratio" defaultValue={scalar(axle, "ratio")} /></fieldset><fieldset><legend>Emissions and auxiliary equipment</legend><Field label="Emissions manufacturer" name="emissions_manufacturer" defaultValue={scalar(emissions, "manufacturer")} /><Field label="Emissions model" name="emissions_model" defaultValue={scalar(emissions, "model")} /><Field label="Body or upfit" name="body_or_upfit" defaultValue={scalar(auxiliary, "body_or_upfit")} /><Field label="PTO or hydraulic equipment" name="pto_or_hydraulic" defaultValue={scalar(auxiliary, "pto_or_hydraulic")} /></fieldset><button className="button primary">Save equipment details</button></form></details>}</Panel>
      {!isRetired && (can("assets.manage") || can("assets.status") || can("maintenance.manage") || can("maintenance.execute")) ? <Panel title="Record meter"><form className="compact-form" onSubmit={(event) => void meter(event)}><SelectField label="Existing meter" name="meter_id" value={selectedMeter} onChange={setSelectedMeter} options={meters.rows.map((meterRow) => ({ value: identifier(meterRow), label: `${text(meterRow, "name")} · ${text(meterRow, "current_value")} ${text(meterRow, "unit")}` }))} />{!selectedMeter && <><SelectField label="New meter type" name="meter_type" value={meterType} onChange={setMeterType} required options={[{ value: "odometer", label: "Odometer" }, { value: "engine_hours", label: "Engine hours" }]} /><SelectField label="Unit" name="unit" value={meterUnit} onChange={setMeterUnit} required options={[{ value: "mi", label: "Miles" }, { value: "km", label: "Kilometres" }, { value: "h", label: "Hours" }]} /></>}<Field label="Reading" name="meter_value" type="number" inputMode="decimal" min="0" step="0.1" required /><button className="button primary">Record meter</button></form></Panel> : <Panel title="Current meters"><RecordList rows={meters.rows} empty="No meters recorded." render={(meterRow) => <div className="list-row"><Gauge /><span><strong>{text(meterRow, "name")}</strong><small>{text(meterRow, "current_value")} {text(meterRow, "unit")}</small></span></div>} /></Panel>}</div>
    <AssetDocuments assetId={id} retired={isRetired} />
    <Panel title="Meter history"><div className="table-scroll"><table><thead><tr><th>Observed</th><th>Meter</th><th>Value</th><th>Source</th><th>Quality</th>{(can("assets.manage") || can("assets.status")) && <th>Correction</th>}</tr></thead><tbody>{readings.map((reading) => <tr key={identifier(reading)}><td>{formatDate(reading.observed_at)}</td><td>{text(reading, "meter_name")}</td><td>{text(reading, "value")} {text(reading, "unit")}</td><td>{text(reading, "source")}</td><td><Status value={text(reading, "quality", "status")} /></td>{(can("assets.manage") || can("assets.status")) && <td><button className="button secondary" onClick={() => void correctMeter(reading)}>Correct meter</button></td>}</tr>)}</tbody></table>{!meters.loading && !readings.length && <Empty>No meter readings found.</Empty>}</div></Panel>
    <Panel title="Complete asset history"><RecordList rows={history.rows} loading={history.loading} empty="No history events found." render={(event) => <AssetTimelineEvent event={event} />} /></Panel>
  </Page>;
}

function AssetDocuments({ assetId, retired }: { assetId: string; retired: boolean }) {
  const { can } = useApp();
  const documents = useCollection(can("documents.view") ? query("/api/v1/documents/", { asset_id: assetId }) : "", "documents");
  const [queryText, setQueryText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const matches = useCollection(can("documents.view") && submittedQuery.length >= 2 ? query("/api/v1/documents/search/", { asset_id: assetId, q: submittedQuery }) : "", "results", "matches");
  const [approving, setApproving] = useState<Json | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get("file");
    if (!(file instanceof File) || file.size === 0) { setError("Choose a PDF manual to upload."); return; }
    data.set("asset_id", assetId);
    try {
      await api("/api/v1/documents/", { method: "POST", body: data });
      form.reset(); setNotice("Document uploaded and quarantined for security review."); setError(""); await documents.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function approve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!approving) return;
    const data = new FormData(event.currentTarget);
    try {
      await api(`/api/v1/documents/${identifier(approving)}/approve/`, { method: "POST", json: { review_note: value(data, "review_note"), security_review_reference: value(data, "security_review_reference") } });
      setApproving(null); setNotice("Document approved and queued for text extraction."); setError(""); await documents.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (!can("documents.view")) return null;
  return <>
    {notice && <Notice tone="success">{notice}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    {approving && <Panel title={`Approve ${text(approving, "title", "original_name")}`}><form className="form-grid" onSubmit={(event) => void approve(event)}><Field label="Review note" name="review_note" required hint="Confirm the source and intended truck/equipment applicability." /><Field label="Security review reference" name="security_review_reference" required hint="Record the approved malware/security review reference." /><div className="row-actions"><button className="button primary"><Check /> Approve and index</button><button type="button" className="button secondary" onClick={() => setApproving(null)}>Cancel</button></div></form></Panel>}
    <Panel title="Truck documents" action={can("documents.manage") && !retired ? <details className="action-details"><summary className="button secondary"><FileUp /> Add PDF manual</summary><form className="popover-form" onSubmit={(event) => void upload(event)}><Field label="PDF manual" name="file" type="file" accept="application/pdf,.pdf" required /><Field label="Document title" name="title" required /><SelectField label="Category" name="category" required options={[{ value: "owner_manual", label: "Owner manual" }, { value: "service_manual", label: "Service manual" }, { value: "specification", label: "Specification" }, { value: "torque_specification", label: "Torque specification" }, { value: "parts_manual", label: "Parts manual" }, { value: "troubleshooting", label: "Troubleshooting" }, { value: "wiring_diagram", label: "Wiring diagram" }, { value: "technical_document", label: "Technical document" }]} /><Field label="Manufacturer" name="manufacturer" /><Field label="Model or equipment family" name="model" /><Field label="Engine type" name="engine_type" /><Field label="Revision" name="revision" /><Field label="Source or provenance" name="source" required /><Field label="License or usage note" name="license" /><button className="button primary">Upload for review</button></form></details> : undefined}>
      <p>Approved manuals are searchable by their extracted text. Search results cite the exact document and page; uploaded PDFs stay immutable and are reviewed before indexing.</p>
      <form className="page-search" role="search" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(queryText.trim()); }}><Field label="Search this truck’s documents" name="document_query" value={queryText} onChange={setQueryText} /><button className="button secondary"><Search /> Search manuals</button>{submittedQuery && <button type="button" className="button secondary" onClick={() => { setQueryText(""); setSubmittedQuery(""); }}>Clear</button>}</form>
      {submittedQuery && <RecordList rows={matches.rows} loading={matches.loading} empty={`No approved document text matched “${submittedQuery}”.`} render={(match) => { const citation = isRecord(match.citation) ? match.citation : {}; const downloadUrl = text(citation, "download_url"); return <article className="workflow-row"><div><strong>{text(citation, "document_title", "title")}</strong><small>Page {text(citation, "page_number")} · {text(match, "snippet", "excerpt")}</small></div>{downloadUrl !== "—" ? <a className="button secondary" href={downloadUrl}>Open PDF</a> : <small>Source PDF is unavailable.</small>}</article>; }} />}
      <div className="table-scroll"><table><thead><tr><th>Document</th><th>Category</th><th>Revision</th><th>Processing</th><th>Actions</th></tr></thead><tbody>{documents.rows.map((document) => <tr key={identifier(document)}><td><strong>{text(document, "title", "original_name")}</strong><br /><small>{text(document, "manufacturer")} · {text(document, "model")}</small></td><td>{titleCase(text(document, "category"))}</td><td>{text(document, "revision", "version")}</td><td><Status value={text(document, "status", "processing_status")} /></td><td><div className="row-actions">{text(document, "download_url") !== "—" && <a className="button secondary" href={text(document, "download_url")}><FileDown /> Download</a>}{can("documents.manage") && /quarantined/i.test(text(document, "status", "processing_status")) && <button className="button secondary" onClick={() => setApproving(document)}>Review</button>}</div></td></tr>)}</tbody></table>{documents.loading ? <Loading /> : !documents.rows.length && <Empty>No manuals have been added for this truck.</Empty>}</div>
    </Panel>
  </>;
}

function AssetTimelineEvent({ event }: { event: Json }) {
  const links = isRecord(event.links) ? event.links : {};
  const context = isRecord(event.context) ? event.context : {};
  const finalMeters = Array.isArray(context.final_meter_readings) ? context.final_meter_readings.filter(isRecord) : [];
  const path = links.component_id ? `/components/${String(links.component_id)}`
    : links.work_order_id ? `/work-orders/${String(links.work_order_id)}`
    : links.maintenance_request_id ? "/requests"
      : links.defect_id ? "/defects"
        : links.maintenance_plan_id ? "/schedule"
          : "";
  const sourceRecord = isRecord(event.source) ? `${titleCase(text(event.source, "type"))} ${text(event.source, "id")}` : "";
  const reason = text(event, "reason", "description");
  const source = reason !== "—" ? reason : sourceRecord || "Recorded in Fleetline";
  return <div className="list-row"><History /><span><strong>{text(event, "label", "number", "action", "event_type", "type")}</strong><small>{source} · {formatDate(event.occurred_at ?? event.created_at)}</small>{text(context, "retirement_disposition") !== "—" && <small>Disposition: {text(context, "retirement_disposition")} · Final meters: {finalMeters.length ? finalMeters.map((meter) => `${text(meter, "meter_name", "kind")} ${text(meter, "value")} ${text(meter, "unit")}`).join(", ") : "none required"}</small>}</span>{text(event, "status") !== "—" && <Status value={text(event, "status")} />}{path && <Link className="button secondary" to={path}>Open source record</Link>}</div>;
}

function Schedule() {
  const packages = useCollection("/api/v1/maintenance/service-packages/?active=true", "service_packages", "packages");
  const plans = useCollection("/api/v1/maintenance/plans/", "plans");
  const location = useLocation();
  const [tab, setTab] = useState("plans");
  const [error, setError] = useState("");
  const [planAsset, setPlanAsset] = useState(() => new URLSearchParams(location.search).get("asset_id") ?? "");
  const [triggerType, setTriggerType] = useState("date");
  const [resetRule, setResetRule] = useState("completion");
  const { bootstrap, can } = useApp();
  const selectedAsset = bootstrap.assets.find((row) => identifier(row) === planAsset);
  const assetMeters = selectedAsset && Array.isArray(selectedAsset.meters) ? selectedAsset.meters.filter(isRecord) : [];

  async function createPackage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const taskTitle = value(data, "task_title");
      await api("/api/v1/maintenance/service-packages/", { method: "POST", json: { name: value(data, "name"), description: value(data, "description"), tasks: taskTitle ? [{ title: taskTitle, required: true }] : [] } });
      form.reset(); await packages.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function createPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const completedDate = value(data, "last_completed_at");
    try {
      await api("/api/v1/maintenance/plans/", { method: "POST", json: {
        asset_id: value(data, "asset_id"), service_package_id: value(data, "service_package_id"), name: value(data, "name"),
        triggers: [{ kind: triggerType, interval: value(data, "interval_value"), grace: value(data, "grace_value") || "0", due_soon_threshold: value(data, "due_soon_threshold") || "0", reset_rule: resetRule, meter_id: optional(value(data, "meter_id")), last_completed_at: triggerType === "date" && completedDate ? new Date(`${completedDate}T00:00`).toISOString() : undefined, last_completed_value: optional(value(data, "last_completed_value")) }],
      } });
      form.reset(); setPlanAsset(""); setTriggerType("date"); setResetRule("completion"); await plans.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function recalculate() {
    try { await api("/api/v1/maintenance/plans/recalculate/", { method: "POST", json: {} }); await plans.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function createWork(plan: Json) {
    try { await api(`/api/v1/maintenance/plans/${identifier(plan)}/create-work-order/`, { method: "POST", json: {} }); await plans.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  return <Page title="Schedule" actions={(can("pm.manage") || can("maintenance.manage")) ? <button className="button secondary" onClick={() => void recalculate()}><RefreshCw /> Recalculate due status</button> : undefined}>
    <Tabs value={tab} onChange={setTab} tabs={[{ id: "plans", label: "Maintenance plans" }, { id: "packages", label: "Service packages" }]}>
      {error && <Notice tone="danger">{error}</Notice>}
      {tab === "plans" ? <>{can("pm.manage") && <Panel title="Create maintenance plan"><form className="form-grid" onSubmit={(event) => void createPlan(event)}><Field label="Plan name" name="name" required /><SelectField label="Asset" name="asset_id" value={planAsset} onChange={setPlanAsset} required options={bootstrap.assets.map((row) => ({ value: identifier(row), label: text(row, "unit_number") }))} /><SelectField label="Service package" name="service_package_id" required options={packages.rows.map((row) => ({ value: identifier(row), label: `${text(row, "name")} · v${text(row, "version")}` }))} /><SelectField label="Trigger" name="trigger_type" value={triggerType} onChange={setTriggerType} options={[{ value: "date", label: "Date" }, { value: "mileage", label: "Mileage" }, { value: "engine_hours", label: "Engine hours" }]} />{triggerType === "date" ? <Field label="Last completed date" name="last_completed_at" type="date" hint="Leave blank if unknown; the initial service will be due." /> : <><SelectField label="Meter" name="meter_id" required options={assetMeters.filter((meter) => text(meter, "kind") === (triggerType === "mileage" ? "odometer" : "engine_hours")).map((meter) => ({ value: identifier(meter), label: `${text(meter, "name")} · ${text(meter, "current_value")} ${text(meter, "unit")}` }))} /><Field label="Last completed reading" name="last_completed_value" type="number" min="0" step="0.1" hint="Leave blank if unknown; the initial service will be due." /></>}<Field label="Interval" name="interval_value" type="number" min="1" required /><Field label="Due soon threshold" name="due_soon_threshold" type="number" min="0" defaultValue="0" required /><Field label="Grace" name="grace_value" type="number" min="0" defaultValue="0" /><SelectField label="Reset from" name="reset_rule" value={resetRule} onChange={setResetRule} required options={[{ value: "completion", label: "Actual completion" }, { value: "scheduled", label: "Computed schedule" }]} /><button className="button primary">Create plan</button></form></Panel>}<Panel title="Preventive maintenance"><RecordList rows={plans.rows} loading={plans.loading} empty="No maintenance plans found." render={(plan) => <div className="workflow-row"><div><Status value={text(plan, "due_status", "status")} /><h3>{text(plan, "asset_unit_number", "asset")} · {text(plan, "service_package_name", "service_package")}</h3><p>{text(plan, "name")} · {planDueDescription(plan)}</p></div>{can("pm.manage") && /due|overdue/i.test(text(plan, "due_status", "status")) && <button className="button primary" onClick={() => void createWork(plan)}>Create planned work</button>}</div>} /></Panel></>
        : <>{can("pm.manage") && <Panel title="Create service package"><form className="form-grid" onSubmit={(event) => void createPackage(event)}><Field label="Package name" name="name" required /><Field label="Description" name="description" /><Field label="Required task" name="task_title" required /><button className="button primary">Create package</button></form></Panel>}<Panel title="Service packages"><RecordList rows={packages.rows} loading={packages.loading} empty="No service packages found." render={(item) => <div className="list-row"><ClipboardCheck /><span><strong>{text(item, "name")}</strong><small>Version {text(item, "version")} · {text(item, "description")}</small></span></div>} /></Panel></>}
    </Tabs>
  </Page>;
}

function planDueDescription(plan: Json): string {
  const reasons = Array.isArray(plan.due_reasons) ? plan.due_reasons.filter(isRecord) : [];
  if (!reasons.length) return `next due ${text(plan, "next_due_value", "next_due_at")}`;
  return reasons.map((reason) => {
    if (reason.due_value) return `next due ${text(reason, "due_value")} ${text(reason, "unit")} (${titleCase(text(reason, "kind"))})`;
    if (reason.due_at) return `next due ${formatDate(reason.due_at)}`;
    return `next due ${text(reason, "message", "status")}`;
  }).join(" · ");
}

function Parts() {
  const { can, refresh } = useApp();
  const parts = useCollection("/api/v1/inventory/parts/", "parts");
  const [selected, setSelected] = useState<Json | null>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const requestedPartId = new URLSearchParams(location.search).get("part_id") ?? "";
  const selectedPart = selected ?? parts.rows.find((row) => identifier(row) === requestedPartId) ?? null;
  const history = useCollection(selectedPart ? `/api/v1/inventory/parts/${identifier(selectedPart)}/history/` : "", "transactions", "history");
  const balances = records(history.data, "balances");
  const [error, setError] = useState("");

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const alternate = value(data, "alternate_number");
      await api("/api/v1/inventory/parts/", { method: "POST", json: {
        number: value(data, "number"), name: value(data, "name"), description: value(data, "description"),
        manufacturer_number: value(data, "manufacturer_number"), barcode: value(data, "barcode"),
        unit_of_measure: value(data, "unit_of_measure") || "each",
        ...(can("financial.manage") ? { default_unit_cost: value(data, "standard_cost") || "0" } : {}),
        cross_references: alternate ? [{ kind: "ALTERNATE", value: alternate }] : [], active: true,
      } });
      form.reset(); await Promise.all([parts.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (!can("inventory.view")) return <AccessDenied />;
  return <Page title="Parts" actions={(can("inventory.transact") || can("financial.manage")) ? <details className="action-details"><summary className="button primary"><Plus /> New part</summary><form className="popover-form" onSubmit={(event) => void create(event)}><Field label="Part number" name="number" required /><Field label="Part name" name="name" required /><Field label="Description" name="description" /><Field label="Manufacturer part number" name="manufacturer_number" /><Field label="Barcode" name="barcode" /><Field label="Alternate part number" name="alternate_number" /><Field label="Unit of measure" name="unit_of_measure" defaultValue="each" required />{can("financial.manage") && <Field label="Standard cost" name="standard_cost" type="number" min="0" step="0.01" />}<button className="button primary">Create part</button></form></details> : undefined}>
    {error && <Notice tone="danger">{error}</Notice>}
    <Panel title="Scan or find a part"><PartIdentifierLookup fallback="Use the part master below to choose manually." onResolved={setSelected} /></Panel>
    <Panel title="Part master"><div className="table-scroll"><table><thead><tr><th>Part number</th><th>Name</th><th>Alternate numbers</th><th>Unit</th>{can("financial.view") && <th>Standard cost</th>}</tr></thead><tbody>{parts.rows.map((row) => <tr key={identifier(row)}><td><button className="link-button" onClick={() => setSelected(row)}>{text(row, "number")}</button></td><td>{text(row, "name")}</td><td>{Array.isArray(row.cross_references) ? row.cross_references.filter(isRecord).map((item) => text(item, "value")).join(", ") : "—"}</td><td>{text(row, "unit_of_measure", "uom")}</td>{can("financial.view") && <td>{text(row, "default_unit_cost", "cost")}</td>}</tr>)}</tbody></table>{parts.loading ? <Loading /> : !parts.rows.length && <Empty>No parts found.</Empty>}</div></Panel>
    {selectedPart && <><Panel title={`${text(selectedPart, "number")} availability`}><div className="table-scroll"><table><thead><tr><th>Physical location</th><th>On hand</th><th>Reserved</th><th>Available</th></tr></thead><tbody>{balances.map((row) => <tr key={identifier(row)}><td>{physicalLocation(row)}</td><td>{text(row, "quantity_on_hand")}</td><td>{text(row, "quantity_reserved")}</td><td><strong>{text(row, "available_quantity")}</strong></td></tr>)}</tbody></table>{history.loading ? <Loading /> : !balances.length && <Empty>No stock balances recorded for this part.</Empty>}</div></Panel><Panel title={`${text(selectedPart, "number")} history`} action={<button className="icon-button" aria-label="Close part history" onClick={() => { setSelected(null); if (requestedPartId) navigate("/parts", { replace: true }); }}><X /></button>}><RecordList rows={history.rows} loading={history.loading} empty="No part transactions found." render={(row) => <div className="list-row"><History /><span><strong>{text(row, "transaction_type", "type")}: {text(row, "quantity")}</strong><small>{physicalLocation(row)} · {formatDate(row.occurred_at ?? row.created_at)}</small></span></div>} /></Panel></>}
  </Page>;
}

function Inventory() {
  const { bootstrap, can } = useApp();
  const stock = useCollection("/api/v1/inventory/stock/", "stock", "balances");
  const warehouses = useCollection("/api/v1/inventory/warehouses/", "warehouses");
  const bins = useCollection("/api/v1/inventory/bins/", "bins");
  const reservations = useCollection("/api/v1/inventory/reservations/", "reservations");
  const counts = useCollection("/api/v1/inventory/counts/", "counts");
  const [action, setAction] = useState("reserve");
  const [selectedHistoryBin, setSelectedHistoryBin] = useState("");
  const binHistory = useCollection(selectedHistoryBin ? `/api/v1/inventory/bins/${selectedHistoryBin}/history/` : "", "transactions");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [selectedPartId, setSelectedPartId] = useState("");
  const [lookupPart, setLookupPart] = useState<Json | null>(null);
  const workOrderId = new URLSearchParams(window.location.search).get("work_order_id") ?? "";
  const allowedActions = [
    ...(can("inventory.transact") || can("inventory.issue") ? [{ id: "reserve", label: "Reserve part" }] : []),
    ...(can("inventory.issue") ? [{ id: "issue", label: "Issue part" }] : []),
    ...(can("inventory.return") ? [{ id: "return", label: "Return part" }] : []),
    ...(can("inventory.count") ? [{ id: "count", label: "Count stock" }] : []),
    ...(can("inventory.adjust") && can("financial.manage") ? [{ id: "adjust", label: "Adjust stock" }] : []),
    ...(can("inventory.adjust") && can("financial.manage") ? [{ id: "correct", label: "Correct stock transaction" }] : []),
  ];
  const activeAction = allowedActions.some((item) => item.id === action) ? action : allowedActions[0]?.id ?? "";
  const partOptions = lookupPart && !bootstrap.parts.some((part) => identifier(part) === identifier(lookupPart))
    ? [...bootstrap.parts, lookupPart]
    : bootstrap.parts;

  async function createWarehouse(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { await api("/api/v1/inventory/warehouses/", { method: "POST", json: { location_id: value(data, "location_id"), code: value(data, "code"), name: value(data, "name") } }); form.reset(); await warehouses.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function createBin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { await api("/api/v1/inventory/bins/", { method: "POST", json: { warehouse_id: value(data, "warehouse_id"), code: value(data, "code"), name: value(data, "name") } }); form.reset(); await bins.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const common = {
      part_id: value(data, "part_id"), bin_id: value(data, "bin_id"), work_order_id: value(data, "work_order_id"),
      quantity: value(data, "quantity"), reason: value(data, "reason"), reservation_id: optional(value(data, "reservation_id")),
      original_transaction_id: optional(value(data, "original_transaction_id")), counted_quantity: optional(value(data, "counted_quantity")),
    };
    try {
      if ((activeAction === "issue" || activeAction === "return") && !navigator.onLine) {
        await queueOperation(activeAction === "issue" ? "stock.issue" : "stock.return", common);
        setMessage("Saved on this device. Stock remains provisional until synchronized.");
      } else {
        const endpoint = activeAction === "reserve" ? "reservations" : activeAction === "issue" ? "issues" : activeAction === "return" ? "returns" : activeAction === "count" ? "counts" : "adjustments";
        const selectedBin = bins.rows.find((row) => identifier(row) === common.bin_id);
        const payload = activeAction === "count" ? { warehouse_id: selectedBin?.warehouse_id, reason: common.reason || "Cycle count", lines: [{ part_id: common.part_id, bin_id: common.bin_id, counted_quantity: common.counted_quantity }] }
          : activeAction === "correct" ? { original_transaction_id: common.original_transaction_id, reason: common.reason }
            : common;
        const result = await api<Json>(`/api/v1/inventory/${endpoint}/`, { method: "POST", json: payload });
        const submittedCount = isRecord(result.count) ? result.count : null;
        setMessage(activeAction === "count" && text(submittedCount ?? undefined, "status") === "PendingApproval"
          ? "Inventory count is pending approval. Stock has not changed."
          : `${titleCase(activeAction)} transaction recorded.`);
      }
      setError("");
      setSelectedPartId("");
      setLookupPart(null);
      form.reset();
      await Promise.all([stock.reload(), reservations.reload(), counts.reload(), binHistory.reload()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function approveCount(count: Json) {
    try {
      await api(`/api/v1/inventory/counts/${identifier(count)}/approve/`, { method: "POST", json: {} });
      setMessage("Inventory count approved and posted."); setError("");
      await Promise.all([counts.reload(), stock.reload(), binHistory.reload()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (!can("inventory.view")) return <AccessDenied />;
  return <Page title="Inventory">
    {activeAction !== "correct" && <Panel title="Scan or find a part"><PartIdentifierLookup fallback="Use the part selector below to choose manually." onResolved={(part) => { setLookupPart(part); setSelectedPartId(identifier(part)); }} /></Panel>}
    {allowedActions.length > 0 && <Panel title="Stock action"><Tabs value={activeAction} onChange={setAction} tabs={allowedActions}>{message && <Notice tone="success">{message}</Notice>}{error && <Notice tone="danger">{error}</Notice>}<form className="form-grid" onSubmit={(event) => void submit(event)}>
      {activeAction !== "correct" && <SelectField label="Part" name="part_id" value={selectedPartId} onChange={setSelectedPartId} required options={partOptions.map((row) => ({ value: identifier(row), label: `${text(row, "number")} · ${text(row, "name")}` }))} />}
      {activeAction !== "correct" && <SelectField label="Bin" name="bin_id" required options={bins.rows.map((row) => ({ value: identifier(row), label: physicalLocationLabel(row) }))} />}
      {!["count", "adjust", "correct"].includes(activeAction) && <Field label="Work order ID" name="work_order_id" defaultValue={workOrderId} required={activeAction !== "return"} />}
      {activeAction === "issue" && <SelectField label="Reservation" name="reservation_id" options={reservations.rows.map((row) => ({ value: identifier(row), label: `${text(row, "part_number", "part")} · ${text(row, "quantity")}` }))} />}
      {activeAction === "return" && <Field label="Original issue transaction ID" name="original_transaction_id" required />}
      {activeAction === "correct" && <Field label="Original stock transaction ID" name="original_transaction_id" required hint="The original remains in history; this creates a linked compensating reversal." />}
      {activeAction === "count" ? <Field label="Counted quantity" name="counted_quantity" type="number" min="0" step="0.001" required /> : activeAction !== "correct" && <Field label={activeAction === "adjust" ? "Quantity change" : "Quantity"} name="quantity" type="number" min={activeAction === "adjust" ? undefined : "0.001"} step="0.001" required />}
      {(activeAction === "adjust" || activeAction === "return" || activeAction === "count" || activeAction === "correct") && <Field label="Reason" name="reason" required />}
      <button className="button primary">{allowedActions.find((item) => item.id === activeAction)?.label}</button>
    </form></Tabs></Panel>}
    {can("inventory.transact") && <div className="split"><Panel title="Create storage area"><form className="compact-form" onSubmit={(event) => void createWarehouse(event)}><SelectField label="Yard" name="location_id" required options={(bootstrap.locations ?? []).map((row) => ({ value: identifier(row), label: `${text(row, "code")} · ${text(row, "name")}` }))} /><Field label="Storage area code" name="code" required hint="For example: PARTS-CAGE." /><Field label="Storage area name" name="name" required /><button className="button secondary">Create storage area</button></form></Panel><Panel title="Create physical location"><form className="compact-form" onSubmit={(event) => void createBin(event)}><SelectField label="Storage area" name="warehouse_id" required options={warehouses.rows.map((row) => ({ value: identifier(row), label: `${text(row, "code")} · ${text(row, "name")}` }))} /><Field label="Physical address" name="code" required hint="For example: CAB-02/SHELF-03/DRAWER-01/BIN-04." /><Field label="Location description" name="name" /><button className="button secondary">Create physical location</button></form></Panel></div>}
    <Panel title="Stock on hand"><div className="table-scroll"><table><thead><tr><th>Part</th><th>Warehouse / bin</th><th>On hand</th><th>Reserved</th><th>Available</th></tr></thead><tbody>{stock.rows.map((row) => <tr key={identifier(row)}><td>{text(row, "part_number", "part")}</td><td>{physicalLocation(row)}</td><td>{text(row, "on_hand", "quantity_on_hand", "quantity")}</td><td>{text(row, "reserved", "reserved_quantity", "quantity_reserved")}</td><td><strong>{text(row, "available", "available_quantity")}</strong></td></tr>)}</tbody></table>{stock.loading ? <Loading /> : !stock.rows.length && <Empty>No stock balances found.</Empty>}</div></Panel>
    <Panel title="Inventory counts"><RecordList rows={counts.rows} loading={counts.loading} empty="No inventory counts found." render={(count) => <article className="workflow-row"><div><Status value={text(count, "status")} /><strong>{text(count, "warehouse_code")}{can("financial.view") && ` · variance value $${text(count, "total_variance_value")}`}</strong><small>{text(count, "reason")} · created {formatDate(count.created_at)}{count.approved_at ? ` · approved ${formatDate(count.approved_at)}` : ""}</small></div>{can("inventory.adjust") && can("financial.manage") && text(count, "status") === "PendingApproval" && text(count, "created_by_id") !== bootstrap.user.id && <button className="button primary" onClick={() => void approveCount(count)}>Approve inventory count</button>}</article>} /></Panel>
    <Panel title="Bin transaction history"><div className="history-filter"><SelectField label="History bin" name="history_bin_id" value={selectedHistoryBin} onChange={setSelectedHistoryBin} options={bins.rows.map((row) => ({ value: identifier(row), label: physicalLocationLabel(row) }))} /></div>{selectedHistoryBin ? <div className="table-scroll"><table><thead><tr><th>Posted</th><th>Transaction</th><th>Part</th><th>Quantity</th><th>Work order</th><th>Reason</th></tr></thead><tbody>{binHistory.rows.map((transaction) => <tr key={identifier(transaction)}><td>{formatDate(transaction.created_at)}</td><td>{titleCase(text(transaction, "type"))}</td><td>{text(transaction, "part_number")}</td><td>{text(transaction, "quantity")}</td><td>{text(transaction, "work_order_id")}</td><td>{text(transaction, "reason")}</td></tr>)}</tbody></table>{binHistory.loading ? <Loading /> : !binHistory.rows.length && <Empty>No transactions found for this bin.</Empty>}</div> : <Empty>Select a bin to review its immutable stock transactions.</Empty>}</Panel>
    {warehouses.rows.length > 0 && <Panel title="Warehouses"><RecordList rows={warehouses.rows} render={(row) => <div className="list-row"><Warehouse /><span><strong>{text(row, "code")} · {text(row, "name")}</strong><small>Physical storage area</small></span></div>} /></Panel>}
  </Page>;
}

function Purchasing({ initialTab = "orders" }: { initialTab?: "orders" | "vendors" | "requests" }) {
  const { bootstrap, can } = useApp();
  const [tab, setTab] = useState<"orders" | "vendors" | "requests">(initialTab);
  const canViewOrders = can("purchasing.manage") || can("purchasing.receive") || can("purchasing.policy");
  const canViewVendors = can("vendors.view") || can("vendors.manage") || can("purchasing.policy");
  const canViewRequests = can("purchasing.request") || can("purchasing.policy");
  const vendors = useCollection(canViewVendors ? "/api/v1/purchasing/vendors/" : "", "vendors");
  const orders = useCollection(canViewOrders ? "/api/v1/purchasing/purchase-orders/" : "", "purchase_orders", "orders");
  const receipts = useCollection(canViewOrders ? "/api/v1/purchasing/receipts/" : "", "receipts");
  const purchaseRequests = useCollection(canViewRequests ? "/api/v1/purchasing/purchase-requests/" : "", "purchase_requests");
  const bins = useCollection("/api/v1/inventory/bins/", "bins");
  const [selectedOrder, setSelectedOrder] = useState("");
  const [selectedPurchaseRequest, setSelectedPurchaseRequest] = useState("");
  const [poPartId, setPoPartId] = useState("");
  const [poQuantity, setPoQuantity] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const selected = orders.rows.find((row) => identifier(row) === selectedOrder);
  const requestedVendorId = new URLSearchParams(window.location.search).get("vendor_id") ?? "";
  const requestedVendor = vendors.rows.find((row) => identifier(row) === requestedVendorId);
  const lines = selected && Array.isArray(selected.lines) ? selected.lines.filter(isRecord) : [];

  async function createVendor(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { await api("/api/v1/purchasing/vendors/", { method: "POST", json: { code: value(data, "code"), name: value(data, "name"), email: value(data, "email"), phone: value(data, "phone"), active: true } }); form.reset(); await vendors.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function createOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/purchasing/purchase-orders/", { method: "POST", json: { vendor_id: value(data, "vendor_id"), notes: value(data, "notes"), lines: [{ part_id: value(data, "part_id"), purchase_request_id: optional(value(data, "purchase_request_id")), ordered_quantity: value(data, "quantity"), quantity: value(data, "quantity"), unit_cost: value(data, "unit_cost") }] } });
      setMessage(selectedPurchaseRequest ? "Purchase order created and linked to the approved purchase request." : "Purchase order created.");
      setSelectedPurchaseRequest(""); setPoPartId(""); setPoQuantity(""); form.reset();
      await Promise.all([orders.reload(), purchaseRequests.reload()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function createPurchaseRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/purchasing/purchase-requests/", { method: "POST", json: { part_id: value(data, "part_id"), quantity: value(data, "quantity"), reason: value(data, "reason"), needed_by: optional(value(data, "needed_by")) } });
      setMessage("Purchase request submitted for approval."); setError(""); form.reset(); await purchaseRequests.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function transitionPurchaseRequest(request: Json, target: "Approved" | "Rejected" | "Cancelled") {
    let reason = "";
    if (target !== "Approved") {
      reason = window.prompt(`Reason to ${target === "Rejected" ? "reject" : "cancel"} this purchase request`)?.trim() ?? "";
      if (!reason) return;
    }
    try {
      await api(`/api/v1/purchasing/purchase-requests/${identifier(request)}/transition/`, { method: "POST", json: { target, reason } });
      setMessage(`Purchase request ${target.toLowerCase()}.`); setError(""); await purchaseRequests.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  function selectPurchaseRequest(requestId: string) {
    setSelectedPurchaseRequest(requestId);
    const request = purchaseRequests.rows.find((row) => identifier(row) === requestId);
    if (request) { setPoPartId(text(request, "part_id")); setPoQuantity(text(request, "quantity")); }
  }

  async function transitionOrder(order: Json, status: string) {
    try { await api(`/api/v1/purchasing/purchase-orders/${identifier(order)}/transition/`, { method: "POST", json: { status, target: status } }); await orders.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function receive(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const lineId = value(data, "line_id");
    try {
      await api("/api/v1/purchasing/receipts/", { method: "POST", json: { purchase_order_id: selectedOrder, received_at: new Date().toISOString(), lines: [{ purchase_order_line_id: lineId, line_id: lineId, received_quantity: value(data, "quantity"), quantity: value(data, "quantity"), bin_id: value(data, "bin_id") }] } });
      setMessage("Receipt posted. Only the quantity received was added to stock."); form.reset(); await Promise.all([orders.reload(), receipts.reload()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function reverse(receipt: Json) {
    const reason = window.prompt("Reason for reversing this receipt"); if (!reason) return;
    try { await api(`/api/v1/purchasing/receipts/${identifier(receipt)}/reverse/`, { method: "POST", json: { reason } }); setMessage("Receipt reversed with compensating stock transactions."); await Promise.all([orders.reload(), receipts.reload()]); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  function linkedOrderFor(requestId: string): Json | undefined {
    return orders.rows.find((order) => Array.isArray(order.lines) && order.lines.some((line) => isRecord(line) && text(line, "purchase_request_id") === requestId));
  }

  const purchasingTabs = [
    ...(canViewOrders ? [{ id: "orders", label: "Purchase orders" }] : []),
    ...(canViewRequests ? [{ id: "requests", label: "Purchase requests" }] : []),
    ...(canViewVendors ? [{ id: "vendors", label: "Vendors" }] : []),
  ];

  return <Page title={tab === "vendors" ? "Vendors" : tab === "requests" ? "Purchase requests" : "Purchase orders"}>
    <Tabs value={tab} onChange={(next) => setTab(next as typeof tab)} tabs={purchasingTabs}>
      {message && <Notice tone="success">{message}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
      {tab === "vendors" ? <>{can("vendors.manage") && <Panel title="Create vendor"><form className="form-grid" onSubmit={(event) => void createVendor(event)}><Field label="Vendor code" name="code" required /><Field label="Vendor name" name="name" required /><Field label="Email" name="email" type="email" /><Field label="Phone" name="phone" type="tel" /><button className="button primary">Create vendor</button></form></Panel>}<Panel title="Vendors"><RecordList rows={vendors.rows} loading={vendors.loading} empty="No vendors found." render={(row) => <div className="list-row"><Warehouse /><span><strong>{text(row, "code")} · {text(row, "name")}</strong><small>{text(row, "email")} · {text(row, "phone")}</small></span></div>} /></Panel></>
      : tab === "requests" ? <>{can("purchasing.request") && <Panel title="Create purchase request"><form className="form-grid" onSubmit={(event) => void createPurchaseRequest(event)}><SelectField label="Part" name="part_id" required options={bootstrap.parts.map((row) => ({ value: identifier(row), label: `${text(row, "number")} · ${text(row, "name")}` }))} /><Field label="Quantity requested" name="quantity" type="number" min="0.001" step="0.001" required /><Field label="Reason" name="reason" required /><Field label="Needed by" name="needed_by" type="date" /><button className="button primary">Create purchase request</button></form></Panel>}<Panel title="Purchase requests"><RecordList rows={purchaseRequests.rows} loading={purchaseRequests.loading} empty="No purchase requests found." render={(request) => { const linkedOrder = linkedOrderFor(identifier(request)); return <article className="workflow-row"><div><Status value={text(request, "status")} /><strong>{text(request, "part_number")} · {text(request, "quantity")}</strong><small>{text(request, "reason")} · needed {request.needed_by ? formatDate(request.needed_by) : "date not specified"} · requested {formatDate(request.created_at)}</small>{linkedOrder && <small>Converted to <button className="link-button" onClick={() => { setSelectedOrder(identifier(linkedOrder)); setTab("orders"); }}>{text(linkedOrder, "number")}</button></small>}</div><div className="row-actions">{can("purchasing.approve") && text(request, "status") === "Submitted" && text(request, "requested_by_id") !== bootstrap.user.id && <button className="button primary" onClick={() => void transitionPurchaseRequest(request, "Approved")}>Approve request</button>}{can("purchasing.approve") && text(request, "status") === "Submitted" && <button className="button secondary" onClick={() => void transitionPurchaseRequest(request, "Rejected")}>Reject request</button>}{can("purchasing.request") && /submitted|approved/i.test(text(request, "status")) && (text(request, "requested_by_id") === bootstrap.user.id || can("purchasing.manage")) && <button className="button secondary" onClick={() => void transitionPurchaseRequest(request, "Cancelled")}>Cancel request</button>}{can("purchasing.manage") && text(request, "status") === "Approved" && <button className="button primary" onClick={() => { selectPurchaseRequest(identifier(request)); setTab("orders"); }}>Create purchase order</button>}</div></article>; }} /></Panel></>
      : <>{can("purchasing.manage") && can("financial.manage") && <Panel title="Create purchase order"><form className="form-grid" onSubmit={(event) => void createOrder(event)}><SelectField label="Vendor" name="vendor_id" required options={vendors.rows.map((row) => ({ value: identifier(row), label: text(row, "name") }))} /><SelectField label="Source purchase request" name="purchase_request_id" value={selectedPurchaseRequest} onChange={selectPurchaseRequest} options={purchaseRequests.rows.filter((row) => text(row, "status") === "Approved").map((row) => ({ value: identifier(row), label: `${text(row, "part_number")} · ${text(row, "quantity")} · ${text(row, "reason")}` }))} /><SelectField label="Part" name="part_id" value={poPartId} onChange={setPoPartId} required options={bootstrap.parts.map((row) => ({ value: identifier(row), label: `${text(row, "number")} · ${text(row, "name")}` }))} /><Field label="Quantity ordered" name="quantity" type="number" min="0.001" step="0.001" value={poQuantity} onChange={setPoQuantity} required /><Field label="Unit cost" name="unit_cost" type="number" min="0" step="0.01" required /><Field label="Notes" name="notes" /><button className="button primary">Create purchase order</button></form></Panel>}
      <Panel title="Purchase orders"><RecordList rows={orders.rows} loading={orders.loading} empty="No purchase orders found." render={(row) => <article className="workflow-row"><button className="row-main" onClick={() => setSelectedOrder(identifier(row))}><Status value={text(row, "status")} /><strong>{text(row, "number")} · {text(row, "vendor_name", "vendor")}</strong><small>Ordered {formatDate(row.created_at)}{can("financial.view") && ` · Total $${text(row, "total")}`}</small></button><div className="row-actions">{can("purchasing.manage") && can("financial.manage") && text(row, "status") === "Draft" && <button className="button secondary" onClick={() => void transitionOrder(row, "Submitted")}>Submit purchase order</button>}{can("purchasing.approve") && can("financial.manage") && text(row, "status") === "Submitted" && text(row, "created_by_id") !== bootstrap.user.id && <button className="button secondary" onClick={() => void transitionOrder(row, "Approved")}>Approve purchase order</button>}{can("purchasing.manage") && can("financial.manage") && text(row, "status") === "Approved" && <button className="button secondary" onClick={() => void transitionOrder(row, "Sent")}>Send purchase order</button>}{can("purchasing.receive") && /sent|partiallyreceived/i.test(text(row, "status")) && <button className="button primary" onClick={() => setSelectedOrder(identifier(row))}>Receive items</button>}</div></article>} /></Panel>
      {selected && can("purchasing.receive") && /sent|partiallyreceived/i.test(text(selected, "status")) && <Panel title={`Receive ${text(selected, "number")}`}><form className="form-grid" onSubmit={(event) => void receive(event)}><SelectField label="Purchase order line" name="line_id" required options={lines.map((row) => ({ value: identifier(row), label: `${text(row, "part_number", "part")} · ${text(row, "quantity_remaining", "quantity_ordered")} remaining` }))} /><SelectField label="Receiving bin" name="bin_id" required options={bins.rows.map((row) => ({ value: identifier(row), label: text(row, "code", "name") }))} /><Field label="Quantity received" name="quantity" type="number" min="0.001" step="0.001" required /><button className="button primary">Post partial receipt</button></form></Panel>}
      <Panel title="Receipt history"><RecordList rows={receipts.rows} loading={receipts.loading} empty="No receipts found." render={(row) => <div className="workflow-row"><div><Status value={text(row, "status")} /><strong>{text(row, "number")} · {text(row, "purchase_order_number", "purchase_order")}</strong><small>{formatDate(row.received_at ?? row.created_at)}</small></div>{can("purchasing.receive") && !row.reversal_of_id && !row.reversal_of && !/reversed|reversal/i.test(text(row, "status", "type")) && <button className="button secondary" onClick={() => void reverse(row)}>Reverse receipt</button>}</div>} /></Panel></>}
      {tab === "vendors" && requestedVendor && <Panel title={`${text(requestedVendor, "name")} vendor details`}><dl className="details"><dt>Vendor code</dt><dd>{text(requestedVendor, "code")}</dd><dt>Contact</dt><dd>{text(requestedVendor, "contact_name")}</dd><dt>Email</dt><dd>{text(requestedVendor, "email")}</dd><dt>Phone</dt><dd>{text(requestedVendor, "phone")}</dd>{can("financial.view") && <><dt>Payment terms</dt><dd>{text(requestedVendor, "payment_terms")}</dd></>}</dl></Panel>}
    </Tabs>
  </Page>;
}

function Alerts() {
  const { can } = useApp();
  const alerts = useCollection("/api/v1/maintenance/alerts/", "maintenance_alerts", "alerts");
  const [error, setError] = useState("");
  async function transition(row: Json, status: string) {
    try { await api(`/api/v1/maintenance/alerts/${identifier(row)}/transition/`, { method: "POST", json: { status } }); await alerts.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }
  return <Page title="Alerts">{error && <Notice tone="danger">{error}</Notice>}<Panel title="Maintenance alerts"><RecordList rows={alerts.rows} loading={alerts.loading} empty="No maintenance alerts need attention." render={(row) => <article className="workflow-row"><div><Status value={text(row, "status", "severity")} /><h3>{text(row, "title", "code")} · {text(row, "asset_unit_number", "asset")}</h3><p>{text(row, "description", "message")}</p><small>{formatDate(row.observed_at ?? row.created_at)}</small></div><div className="row-actions"><Link className="button secondary" to={`/assets/${text(row, "asset_id")}`}>View asset</Link>{can("maintenance.manage") && /new|needsreview/i.test(text(row, "status")) && <button className="button secondary" onClick={() => void transition(row, "Acknowledged")}>Acknowledge</button>}{can("maintenance.manage") && text(row, "status") === "Acknowledged" && <button className="button primary" onClick={() => void transition(row, "Converted")}>Create maintenance request</button>}</div></article>} /></Panel></Page>;
}

function Integrations() {
  const { bootstrap, can } = useApp();
  const devices = useCollection(can("integrations.manage") ? "/api/v1/integrations/devices/" : "", "devices");
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { const result = await api<Json>("/api/v1/integrations/devices/", { method: "POST", json: { name: value(data, "name"), vendor: value(data, "vendor") || "AutoPi", model: value(data, "model"), serial_number: value(data, "serial_number"), external_id: value(data, "external_id") } }); setMessage(`Device registered. Save its ingestion token now: ${text(isRecord(result.device) ? result.device : result, "token")}`); form.reset(); await devices.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function associate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { await api(`/api/v1/integrations/devices/${selected}/associate/`, { method: "POST", json: { asset_id: value(data, "asset_id"), effective_from: new Date().toISOString() } }); setMessage("Device assignment recorded with an effective start time."); form.reset(); await devices.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function ingest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    let payload: unknown;
    try { payload = JSON.parse(value(data, "payload")); }
    catch { setError("Payload must be valid JSON."); return; }
    try { await api("/api/v1/integrations/telematics/autopi/v1/messages/", { method: "POST", headers: { "X-Device-Token": value(data, "device_token") }, json: payload }); setMessage("AutoPi message accepted through the public adapter."); form.reset(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  if (!can("integrations.manage")) return <AccessDenied />;
  return <Page title="Devices and integrations" actions={<Link className="button secondary" to="/data-quality"><Database /> Data quality</Link>}>
    {message && <Notice tone="success">{message}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    <div className="split"><Panel title="Register device"><form className="compact-form" onSubmit={(event) => void create(event)}><Field label="Device name" name="name" required /><Field label="Vendor" name="vendor" defaultValue="AutoPi" required /><Field label="Model" name="model" required /><Field label="Serial number" name="serial_number" required /><Field label="External device ID" name="external_id" required /><button className="button primary">Register device</button></form></Panel>
      <Panel title="Assign device to asset"><form className="compact-form" onSubmit={(event) => void associate(event)}><SelectField label="Device" name="device_id" value={selected} onChange={setSelected} required options={devices.rows.map((row) => ({ value: identifier(row), label: `${text(row, "vendor")} ${text(row, "serial_number", "external_id")}` }))} /><SelectField label="Asset" name="asset_id" required options={bootstrap.assets.map((row) => ({ value: identifier(row), label: text(row, "unit_number") }))} /><button className="button primary">Assign device</button></form></Panel></div>
    <Panel title="Registered devices"><RecordList rows={devices.rows} loading={devices.loading} empty="No devices registered." render={(row) => <div className="list-row"><Cloud /><span><strong>{text(row, "vendor")} {text(row, "model")}</strong><small>{text(row, "serial_number")} · {text(row, "asset", "status")}</small></span><Status value={text(row, "status")} /></div>} /></Panel>
    <details className="advanced"><summary>Submit recorded AutoPi payload</summary><form className="compact-form" onSubmit={(event) => void ingest(event)}><Field label="Device ingestion token" name="device_token" type="password" autoComplete="off" required /><Field label="Payload JSON" name="payload" as="textarea" spellCheck={false} required /><button className="button secondary">Ingest meter payload</button></form></details>
  </Page>;
}

function DataQuality() {
  const { can } = useApp();
  const issues = useCollection(can("reports.integration") ? "/api/v1/integrations/data-quality/" : "", "exceptions", "issues", "data_quality");
  if (!can("reports.integration")) return <AccessDenied />;
  return <Page title="Data quality"><Panel title="Readings and devices needing review"><RecordList rows={issues.rows} loading={issues.loading} empty="No data-quality issues found." render={(row) => <div className="workflow-row"><div><Status value={text(row, "status", "severity")} /><h3>{text(row, "title", "issue_type", "reason")}</h3><p>{text(row, "message", "description")}</p><small>{text(row, "asset_unit_number", "device_serial")} · {formatDate(row.observed_at ?? row.created_at)}</small></div>{row.asset_id ? <Link className="button secondary" to={`/assets/${String(row.asset_id)}`}>View history</Link> : null}</div>} /></Panel></Page>;
}

function Reports() {
  const { can } = useApp();
  const canReport = ["reports.shop", "reports.all", "reports.executive", "reports.inventory", "reports.purchasing", "reports.integration"].some(can);
  const report = useResource(canReport ? "/api/v1/reports/operations/" : "");
  const summary = isRecord(report.data?.summary) ? report.data.summary : {};
  const sources = records(report.data, "source_records");
  const [error, setError] = useState("");
  const [importResult, setImportResult] = useState<Json | null>(null);

  async function importCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const input = form.elements.namedItem("file") as HTMLInputElement; const file = input.files?.[0];
    if (!file) return;
    const data = new FormData(); data.append("file", file);
    try { const result = await api<Json>("/api/v1/import/assets/", { method: "POST", body: data }); setImportResult(result); setError(""); form.reset(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function downloadExport() {
    try {
      const response = await fetch("/api/v1/export/", { credentials: "same-origin" });
      if (!response.ok) throw new Error(`Export failed (${response.status})`);
      const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `fleetline-export-${new Date().toISOString().slice(0, 10)}.json`; anchor.click(); URL.revokeObjectURL(url);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (!canReport) return <AccessDenied />;
  if (report.loading) return <Loading />;
  return <Page title="Reports" actions={can("export.all") && can("financial.export") ? <button className="button secondary" onClick={() => void downloadExport()}><FileDown /> Export data</button> : undefined}>
    {report.error && <Notice tone="danger">{report.error}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    <section className="metrics" aria-label="Report summary"><Metric icon={<Truck />} label="Out of service" value={String(summary.out_of_service ?? 0)} /><Metric icon={<Wrench />} label="Open work orders" value={String(summary.open_work_orders ?? 0)} />{can("financial.view") && <Metric icon={<Package />} label="Part cost" value={`$${summary.part_cost ?? "0"}`} />}</section>
    <Panel title="Source records"><div className="table-scroll"><table><thead><tr><th>Work order</th><th>Asset</th><th>Status</th><th>Summary</th></tr></thead><tbody>{sources.map((row) => <tr key={identifier(row)}><td><Link to={`/work-orders/${identifier(row)}`}>{text(row, "number")}</Link></td><td>{text(row, "asset__unit_number", "asset_unit_number")}</td><td><Status value={text(row, "status")} /></td><td>{text(row, "summary")}</td></tr>)}</tbody></table>{!sources.length && <Empty>No source records found.</Empty>}</div></Panel>
    {can("import.manage") && <Panel title="Import assets from CSV"><form className="compact-form" onSubmit={(event) => void importCsv(event)}><Field label="CSV file" name="file" type="file" accept="text/csv,.csv" required hint="Columns: unit_number, asset_type, VIN. location_code remains accepted for future multi-yard imports." /><button className="button primary"><FileUp /> Import assets</button></form>{importResult && <section className="import-result" role="status" aria-live="polite"><h3>Import complete</h3><p><strong>{text(importResult, "created_count")} created</strong> · <strong>{text(importResult, "rejected_count")} rejected</strong></p>{records(importResult, "rejected").length > 0 && <ul>{records(importResult, "rejected").map((rejected, index) => <li key={`${text(rejected, "row")}-${index}`}>Row {text(rejected, "row")}: {text(rejected, "error")}</li>)}</ul>}</section>}</Panel>}
  </Page>;
}

function SearchPage() {
  const search = new URLSearchParams(window.location.search).get("q") ?? "";
  const [q, setQ] = useState(search);
  const result = useResource(search.length >= 2 ? query("/api/v1/search/", { q: search }) : "");
  const rows = records(result.data, "results");
  const navigate = useNavigate();
  const pathFor = (row: Json) => row.type === "asset" ? `/assets/${row.id}` : row.type === "work_order" ? `/work-orders/${row.id}` : row.type === "part" ? `/parts?part_id=${encodeURIComponent(String(row.id))}` : row.type === "vendor" ? `/vendors?vendor_id=${encodeURIComponent(String(row.id))}` : row.type === "component" ? `/components/${row.id}` : "/";
  return <Page title="Search"><form className="page-search" role="search" onSubmit={(event) => { event.preventDefault(); navigate(`/search?q=${encodeURIComponent(q.trim())}`); }}><Field label="Search assets, work orders, parts, vendors, and components" name="q" value={q} onChange={setQ} /><button className="button primary"><Search /> Search</button></form><Panel title={search ? `Results for “${search}”` : "Results"}><RecordList rows={rows} loading={result.loading} empty={search.length < 2 ? "Enter at least two characters." : "No matching records found."} render={(row) => <Link className="list-row" to={pathFor(row)}><Search /><span><strong>{text(row, "label")}</strong><small>{titleCase(text(row, "type"))} · {text(row, "detail")}</small></span><ChevronRight /></Link>} /></Panel></Page>;
}

function Administration() {
  const { bootstrap, can, refresh } = useApp();
  const webhooks = useCollection(can("webhooks.manage") ? "/api/v1/webhooks/" : "", "webhooks");
  const [deliveryStatus, setDeliveryStatus] = useState("");
  const deliveries = useCollection(can("webhooks.manage") ? query("/api/v1/webhooks/deliveries/", { status: optional(deliveryStatus) }) : "", "deliveries");
  const users = useCollection(can("admin.users") ? "/api/v1/users/" : "", "users");
  const apiTokens = useCollection(can("admin.users") ? "/api/v1/api-tokens/" : "", "api_tokens");
  const roles = useCollection(can("admin.users") ? "/api/v1/roles/" : "", "roles");
  const locations = useCollection(can("admin.users") || can("admin.config") ? "/api/v1/locations/" : "", "locations");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [tokenSecret, setTokenSecret] = useState("");
  const [mfaProvisioning, setMfaProvisioning] = useState<{ secret: string; uri: string } | null>(null);
  const [administrationLoadedAt] = useState(() => Date.now());

  async function disableUser(user: Json) {
    if (!window.confirm(`Disable ${text(user, "name", "username")} and revoke future authorized activity?`)) return;
    try { await api(`/api/v1/users/${identifier(user)}/disable/`, { method: "POST", json: {} }); setMessage("User disabled and access revoked."); await users.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function createWebhook(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try { const result = await api<Json>("/api/v1/webhooks/", { method: "POST", json: { name: value(data, "name"), url: value(data, "url"), event_types: value(data, "event_types").split(",").map((item) => item.trim()).filter(Boolean) } }); setMessage(`Webhook created. Save its signing secret now: ${text(isRecord(result.webhook) ? result.webhook : result, "signing_secret")}`); form.reset(); await webhooks.reload(); }
    catch (caught) { setError(errorMessage(caught)); }
  }

  async function rotateWebhookSecret(webhook: Json) {
    try {
      const result = await api<Json>(`/api/v1/webhooks/${identifier(webhook)}/rotate-secret/`, { method: "POST", json: {} });
      const updated = isRecord(result.webhook) ? result.webhook : result;
      setMessage(result.secret_recoverable === true ? `Webhook signing secret rotated. Save it now: ${text(updated, "signing_secret")}` : text(result, "message"));
      setError(""); await webhooks.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function changeWebhookStatus(webhook: Json) {
    const status = webhook.active === true ? "inactive" : "active";
    try {
      await api(`/api/v1/webhooks/${identifier(webhook)}/status/`, { method: "POST", json: { status } });
      setMessage(`Webhook ${status === "active" ? "activated" : "deactivated"}.`); setError(""); await Promise.all([webhooks.reload(), deliveries.reload()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function retryWebhookDelivery(delivery: Json) {
    try {
      await api(`/api/v1/webhooks/deliveries/${identifier(delivery)}/retry/`, { method: "POST", json: {} });
      setMessage("Webhook delivery queued for retry."); setError(""); await deliveries.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const firstName = value(data, "first_name"); const lastName = value(data, "last_name"); const locationId = value(data, "default_location_id");
    try {
      const result = await api<Json>("/api/v1/users/", { method: "POST", json: { username: value(data, "username"), name: `${firstName} ${lastName}`.trim(), first_name: firstName, last_name: lastName, password: value(data, "password"), default_location: optional(locationId), default_location_id: optional(locationId), role_slugs: [value(data, "role_slug")] } });
      const provisioning = isRecord(result.mfa_provisioning) ? result.mfa_provisioning : undefined;
      setMfaProvisioning(result.secret_recoverable === true && provisioning ? { secret: text(provisioning, "secret"), uri: text(provisioning, "otpauth_uri") } : null);
      setMessage(result.secret_recoverable === true ? "User created. Save the MFA setup information now." : "User created with the selected role and location."); setError(""); form.reset(); await users.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function resetUserMfa(user: Json) {
    const reason = window.prompt(`Reason for resetting MFA for ${text(user, "username")}`)?.trim() ?? "";
    if (!reason) return;
    try {
      const result = await api<Json>(`/api/v1/users/${identifier(user)}/mfa/`, { method: "POST", json: { reason } });
      const provisioning = isRecord(result.mfa_provisioning) ? result.mfa_provisioning : undefined;
      setMfaProvisioning(result.secret_recoverable === true && provisioning ? { secret: text(provisioning, "secret"), uri: text(provisioning, "otpauth_uri") } : null);
      setMessage(result.secret_recoverable === true ? "MFA reset. Save the new setup information now." : text(result, "message")); setError(""); await users.reload();
    } catch (caught) { setMfaProvisioning(null); setError(errorMessage(caught)); }
  }

  async function createLocation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/locations/", { method: "POST", json: { code: value(data, "code"), name: value(data, "name") } });
      setMessage("Location created."); setError(""); form.reset(); await Promise.all([locations.reload(), refresh()]);
    } catch (caught) { setError(errorMessage(caught)); }
  }

  async function createApiToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const expiry = new Date(value(data, "expires_at"));
    const scopes = value(data, "scopes").split(",").map((scope) => scope.trim()).filter(Boolean);
    if (Number.isNaN(expiry.valueOf())) { setError("Expires at must be a valid future date and time."); return; }
    try {
      const result = await api<Json>("/api/v1/api-tokens/", { method: "POST", json: { user_id: value(data, "user_id"), name: value(data, "name"), scopes, expires_at: expiry.toISOString() } });
      setTokenSecret(result.secret_recoverable === true ? text(result, "token") : "");
      setMessage(result.secret_recoverable === true ? "API token created. Copy the secret now." : text(result, "message"));
      setError(""); form.reset(); await apiTokens.reload();
    } catch (caught) { setTokenSecret(""); setError(errorMessage(caught)); }
  }

  async function revokeApiToken(token: Json) {
    const reason = window.prompt(`Reason for revoking ${text(token, "name")}`)?.trim() ?? "";
    if (!reason) return;
    try {
      await api(`/api/v1/api-tokens/${identifier(token)}/revoke/`, { method: "POST", json: { reason } });
      setTokenSecret(""); setMessage("API token revoked."); setError(""); await apiTokens.reload();
    } catch (caught) { setError(errorMessage(caught)); }
  }

  if (!can("admin.users") && !can("webhooks.manage")) return <AccessDenied />;
  return <Page title="Administration" actions={<Link className="button secondary" to="/audit"><ShieldCheck /> Audit history</Link>}>
    {message && <Notice tone="success">{message}</Notice>}{error && <Notice tone="danger">{error}</Notice>}
    {mfaProvisioning && <section className="import-result" role="status" aria-live="assertive"><h2>Copy this MFA setup now</h2><p>Secret: <code>{mfaProvisioning.secret}</code></p><p>Authenticator URI: <code>{mfaProvisioning.uri}</code></p><p>This information will not be shown again.</p></section>}
    {can("admin.users") && <div className="split"><Panel title="Create user"><form className="compact-form" onSubmit={(event) => void createUser(event)}><Field label="First name" name="first_name" required /><Field label="Last name" name="last_name" required /><Field label="Username or email" name="username" autoComplete="off" required /><Field label="Temporary password" name="password" type="password" autoComplete="new-password" minLength={12} required /><SelectField label="Role" name="role_slug" required options={roles.rows.map((role) => ({ value: text(role, "slug"), label: text(role, "name", "slug") }))} /><SelectField label="Default location" name="default_location_id" options={locations.rows.map((location) => ({ value: identifier(location), label: `${text(location, "code")} · ${text(location, "name")}` }))} /><button className="button primary">Create user</button></form></Panel>{can("admin.config") && <Panel title="Create location"><form className="compact-form" onSubmit={(event) => void createLocation(event)}><Field label="Location code" name="code" maxLength={32} required /><Field label="Location name" name="name" maxLength={160} required /><button className="button primary">Create location</button></form><div className="sublist"><RecordList rows={locations.rows} loading={locations.loading} empty="No locations configured." render={(location) => <div className="list-row"><Warehouse /><span><strong>{text(location, "code")} · {text(location, "name")}</strong><small>{location.active === false ? "Inactive" : "Active"}</small></span></div>} /></div></Panel>}</div>}
    {can("admin.users") && <div className="split"><Panel title="Create API token"><form className="compact-form" onSubmit={(event) => void createApiToken(event)}><SelectField label="Token user" name="user_id" required options={users.rows.map((user) => ({ value: identifier(user), label: `${text(user, "name")} · ${text(user, "username")}` }))} /><Field label="Token name" name="name" maxLength={120} required /><Field label="Scopes" name="scopes" required hint="Comma-separated permissions granted to this token, for example reports.shop." /><Field label="Expires at" name="expires_at" type="datetime-local" required /><button className="button primary">Create API token</button></form>{tokenSecret && <section className="import-result" role="status" aria-live="assertive"><h3>Copy this API token now</h3><code>{tokenSecret}</code><p>This secret will not be shown again. Store it in an approved secret manager.</p></section>}</Panel><Panel title="API tokens"><RecordList rows={apiTokens.rows} loading={apiTokens.loading} empty="No API tokens found." render={(token) => { const expired = new Date(String(token.expires_at)).valueOf() <= administrationLoadedAt; const state = token.revoked_at ? "Revoked" : expired ? "Expired" : "Active"; return <article className="workflow-row"><div><Status value={state} /><strong>{text(token, "name")} · {text(token, "prefix")}</strong><small>{text(token, "user")} · {Array.isArray(token.scopes) ? token.scopes.map(String).join(", ") : text(token, "scopes")}</small><small>Created {formatDate(token.created_at)} · expires {formatDate(token.expires_at)} · last used {formatDate(token.last_used_at)}</small></div>{!token.revoked_at && <button className="button safety" onClick={() => void revokeApiToken(token)}>Revoke API token</button>}</article>; }} /></Panel></div>}
    <div className="split">{can("admin.users") && <Panel title="Users and roles"><RecordList rows={users.rows} loading={users.loading} empty="No users found." render={(user) => { const privileged = Array.isArray(user.roles) && user.roles.some((role) => role === "system_admin" || role === "integration_admin"); return <div className="workflow-row"><div><strong>{text(user, "name")}</strong><small>{text(user, "username")} · {Array.isArray(user.roles) ? user.roles.map(String).map(titleCase).join(", ") : ""}</small>{privileged && <small>MFA {user.mfa_configured === true ? "configured" : "not configured"}</small>}</div><div className="row-actions">{privileged && identifier(user) !== bootstrap.user.id && <button className="button secondary" onClick={() => void resetUserMfa(user)}>Reset MFA</button>}<button className="button safety" onClick={() => void disableUser(user)}>Disable user</button></div></div>; }} /></Panel>}{can("webhooks.manage") && <Panel title="Create webhook"><form className="compact-form" onSubmit={(event) => void createWebhook(event)}><Field label="Webhook name" name="name" required /><Field label="Destination URL" name="url" type="url" required /><Field label="Event types" name="event_types" required hint="Comma-separated, for example work_order.closed, stock.issued" /><button className="button primary">Create webhook</button></form></Panel>}</div>
    {can("webhooks.manage") && <Panel title="Webhooks"><RecordList rows={webhooks.rows} loading={webhooks.loading} empty="No webhook subscriptions found." render={(row) => <article className="workflow-row"><div><Status value={row.active ? "Active" : "Inactive"} /><strong>{text(row, "name")}</strong><small>{text(row, "url")} · {Array.isArray(row.event_types) ? row.event_types.join(", ") : ""}</small></div><div className="row-actions"><button className="button secondary" aria-label={`Rotate signing secret for ${text(row, "name")}`} onClick={() => void rotateWebhookSecret(row)}>Rotate secret</button><button className="button secondary" aria-label={`${row.active ? "Deactivate" : "Activate"} ${text(row, "name")}`} onClick={() => void changeWebhookStatus(row)}>{row.active ? "Deactivate" : "Activate"}</button></div></article>} /></Panel>}
    {can("webhooks.manage") && <Panel title="Webhook deliveries"><div className="history-filter"><SelectField label="Delivery status" name="delivery_status" value={deliveryStatus} onChange={setDeliveryStatus} options={[{ value: "pending", label: "Pending" }, { value: "retry", label: "Retry queued" }, { value: "delivered", label: "Delivered" }, { value: "dead", label: "Dead letter" }]} /><p><strong>{text(deliveries.data ?? undefined, "dead_letter_count")} dead-letter deliveries</strong></p></div><RecordList rows={deliveries.rows} loading={deliveries.loading} empty="No webhook deliveries found." render={(delivery) => { const subscription = isRecord(delivery.subscription) ? delivery.subscription : undefined; const event = isRecord(delivery.event) ? delivery.event : undefined; const resource = isRecord(event?.resource) ? event.resource : undefined; return <article className="workflow-row"><div><Status value={text(delivery, "status")} /><strong>{text(subscription, "name")} · {text(event, "type")}</strong><small>{text(resource, "type")} {text(resource, "id")} · attempts {text(delivery, "attempts")} · response {text(delivery, "response_status")}</small><small>{text(delivery, "last_error")} · updated {formatDate(delivery.updated_at)}</small></div>{can("webhooks.manage") && /dead|retry/i.test(text(delivery, "status")) && <button className="button secondary" onClick={() => void retryWebhookDelivery(delivery)}>Retry delivery</button>}</article>; }} /></Panel>}
  </Page>;
}

function Audit() {
  const { can } = useApp();
  const [resourceType, setResourceType] = useState("");
  const events = useCollection(can("audit.view") ? query("/api/v1/audit-events/", { resource_type: optional(resourceType) }) : "", "events");
  if (!can("audit.view")) return <AccessDenied />;
  return <Page title="Audit history"><Panel title="Append-only events" action={<label className="inline-field"><span>Resource type</span><input value={resourceType} onChange={(event) => setResourceType(event.target.value)} placeholder="All resources" /></label>}><div className="table-scroll"><table><thead><tr><th>Occurred</th><th>Actor</th><th>Action</th><th>Resource</th><th>Transition</th><th>Source</th><th>Correlation</th></tr></thead><tbody>{events.rows.map((row) => <tr key={identifier(row)}><td>{formatDate(row.occurred_at)}</td><td>{text(row, "actor")}</td><td>{text(row, "action")}</td><td>{text(row, "resource_type")} · {text(row, "resource_id")}</td><td>{text(row, "previous_state")} → {text(row, "new_state")}</td><td>{text(row, "source")}</td><td>{text(row, "correlation_id")}</td></tr>)}</tbody></table>{events.loading ? <Loading /> : !events.rows.length && <Empty>No audit events found.</Empty>}</div></Panel></Page>;
}

function useCollection(path: string, ...keys: string[]) {
  const resource = useResource(path);
  return { ...resource, rows: records(resource.data, ...keys) };
}

interface CachedResource {
  data: Json;
  user_id: string;
  organization_id: string;
  expires_at: string;
}

function useResource(path: string) {
  const { bootstrap } = useApp();
  const [data, setData] = useState<Json | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [error, setError] = useState("");
  const offlineWorkOrder = /^\/api\/v1\/maintenance\/work-orders\/[0-9a-f-]+\/$/i.test(path);
  const cacheKey = `work-order:${bootstrap.user.organization_id}:${bootstrap.user.id}:${path}`;
  const reload = useCallback(async () => {
    if (!path) { setData(null); setLoading(false); return; }
    setLoading(true);
    try {
      const fresh = await api<Json>(path);
      setData(fresh); setError("");
      const workOrder = isRecord(fresh.work_order) ? fresh.work_order : null;
      if (offlineWorkOrder && workOrder && isWorkAssignedTo(workOrder, bootstrap.user)) {
        const sessionExpiry = new Date(bootstrap.offline_expires_at).valueOf();
        const expiresAt = new Date(Math.min(sessionExpiry, Date.now() + 24 * 60 * 60 * 1000)).toISOString();
        await cachePut(cacheKey, { data: fresh, user_id: bootstrap.user.id, organization_id: bootstrap.user.organization_id, expires_at: expiresAt } satisfies CachedResource);
      }
    } catch (caught) {
      const networkUnavailable = !navigator.onLine || caught instanceof ApiError && caught.status === 0;
      const cached = networkUnavailable && offlineWorkOrder ? await cacheGet<CachedResource>(cacheKey) : undefined;
      const valid = cached && cached.user_id === bootstrap.user.id && cached.organization_id === bootstrap.user.organization_id && new Date(cached.expires_at).valueOf() > Date.now();
      if (valid) { setData(cached.data); setError(""); }
      else setError(errorMessage(caught));
    } finally { setLoading(false); }
  }, [bootstrap.offline_expires_at, bootstrap.user, cacheKey, offlineWorkOrder, path]);
  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);
  return { data, loading, error, reload };
}

function Page({ title, subtitle, actions, narrow = false, children }: { title: string; subtitle?: string; actions?: ReactNode; narrow?: boolean; children: ReactNode }) {
  return <div className={narrow ? "page narrow" : "page"}><header className="page-heading"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{actions && <div className="page-actions">{actions}</div>}</header>{children}</div>;
}

function Panel({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return <section className="panel"><header className="panel-heading"><h2>{title}</h2>{action}</header><div className="panel-body">{children}</div></section>;
}

function Metric({ icon, label, value, tone = "" }: { icon: ReactNode; label: string; value: string; tone?: string }) {
  return <div className={`metric ${tone}`}><span className="metric-icon">{icon}</span><span><small>{label}</small><strong>{value}</strong></span><ChevronRight /></div>;
}

function Notice({ tone, children }: { tone: "success" | "danger" | "info"; children: ReactNode }) {
  const Icon = tone === "success" ? CheckCircle2 : tone === "danger" ? AlertTriangle : Bell;
  return <div className={`notice ${tone}`} role={tone === "danger" ? "alert" : "status"}><Icon /> <span>{children}</span></div>;
}

function Status({ value: statusValue }: { value: string }) {
  const lowered = statusValue.toLowerCase();
  const tone = /closed|complete|available|active|accepted|synced|low|^installed$/.test(lowered) ? "success" : /out|overdue|high|quarantined|rejected|unsafe|conflict/.test(lowered) ? "danger" : /pending|part|medium|due|restricted/.test(lowered) ? "warning" : "neutral";
  const Icon = tone === "success" ? CheckCircle2 : tone === "danger" ? AlertTriangle : tone === "warning" ? Bell : Gauge;
  return <span className={`status ${tone}`}><Icon aria-hidden="true" />{statusValue}</span>;
}

function RecordList({ rows, render, empty = "No records found.", loading = false }: { rows: Json[]; render: (row: Json) => ReactNode; empty?: string; loading?: boolean }) {
  if (loading) return <Loading />;
  return rows.length ? <div className="record-list">{rows.map((row, index) => <div key={identifier(row) || index}>{render(row)}</div>)}</div> : <Empty>{empty}</Empty>;
}

function Tabs({ value: selected, onChange, tabs, children }: { value: string; onChange: (value: string) => void; tabs: { id: string; label: string }[]; children: ReactNode }) {
  const baseId = `tabs-${useId().replace(/:/g, "")}`;
  return <>
    <div className="tabs" role="tablist" aria-label="View options">{tabs.map((tab, index) => <button type="button" role="tab" id={`${baseId}-${tab.id}-tab`} aria-controls={`${baseId}-${tab.id}-panel`} aria-selected={selected === tab.id} tabIndex={selected === tab.id ? 0 : -1} key={tab.id} className={selected === tab.id ? "active" : ""} onClick={() => onChange(tab.id)} onKeyDown={(event) => {
      let next: number;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      onChange(tabs[next].id);
      const buttons = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("[role=tab]");
      buttons?.[next]?.focus();
    }}>{tab.label}</button>)}</div>
    {tabs.map((tab) => <div key={`${tab.id}-panel`} id={`${baseId}-${tab.id}-panel`} role="tabpanel" aria-labelledby={`${baseId}-${tab.id}-tab`} hidden={selected !== tab.id}>{selected === tab.id ? children : null}</div>)}
  </>;
}

type FieldProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange"> & {
  label: string; name: string; hint?: string; hideLabel?: boolean; as?: "textarea"; onChange?: (value: string) => void;
};

function Field({ label, name, hint, hideLabel, as, onChange, ...props }: FieldProps) {
  const id = `field-${name}-${useId().replace(/:/g, "")}`;
  const hintId = hint ? `${id}-hint` : undefined;
  return <label className="field" htmlFor={id}><span className={hideLabel ? "sr-only" : "field-label"}>{label}{props.required && <span aria-hidden="true"> *</span>}</span>{as === "textarea" ? <textarea id={id} name={name} required={props.required} maxLength={props.maxLength} placeholder={props.placeholder} spellCheck={props.spellCheck} aria-describedby={hintId} onChange={onChange ? (event) => onChange(event.target.value) : undefined} defaultValue={props.defaultValue as string | undefined} value={props.value as string | undefined} /> : <input id={id} name={name} {...props} aria-describedby={hintId} onChange={onChange ? (event) => onChange(event.target.value) : undefined} />}{hint && <small id={hintId}>{hint}</small>}</label>;
}

function SelectField({ label, name, options, value: selected, onChange, required }: { label: string; name: string; options: { value: string; label: string }[]; value?: string; onChange?: (value: string) => void; required?: boolean }) {
  const id = `field-${name}-${useId().replace(/:/g, "")}`;
  return <label className="field" htmlFor={id}><span className="field-label">{label}{required && <span aria-hidden="true"> *</span>}</span><select id={id} name={name} value={selected} onChange={onChange ? (event) => onChange(event.target.value) : undefined} required={required}><option value="">Select {label.toLowerCase()}</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
}

function Empty({ children }: { children: ReactNode }) { return <p className="empty">{children}</p>; }
function Loading() { return <div className="loading" role="status"><RefreshCw /> Loading…</div>; }
function AccessDenied() { return <Page title="Access denied"><Notice tone="danger">Your role does not allow access to this page.</Notice></Page>; }
function titleCase(valueToFormat: string): string { return valueToFormat.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatElapsed(startedAt: number, now: number): string {
  const minutes = Math.max(0, Math.floor((now - startedAt) / 60_000));
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m elapsed`;
}

export default App;
