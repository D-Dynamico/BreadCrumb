-- Acme Co. sandbox database. One file shared by the three apps (separate processes)
-- and read by the oracle. Money is stored in paise (integer) to avoid rounding.

-- Login sessions for all apps. Infrastructure, not business data: not audited.
CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Append-only history of every business write in every app. The apps never read
-- it; the oracle uses it for field histories and "never changed" ground truth.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    app TEXT NOT NULL,
    entity TEXT NOT NULL,
    record_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update')),
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    actor TEXT NOT NULL
);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Mailbox -------------------------------------------------------------------
-- password NULL means the mailbox receives mail but nobody can log in to it here.
CREATE TABLE mail_accounts (
    email TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    password TEXT
);
CREATE TABLE mail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    folder TEXT NOT NULL CHECK (folder IN ('inbox', 'sent')),
    from_addr TEXT NOT NULL,
    from_name TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE TABLE mail_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES mail_messages(id),
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL
);
-- Mail to addresses outside @acme.test lands here. Nothing leaves the machine.
CREATE TABLE outbound_mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES mail_messages(id),
    to_addr TEXT NOT NULL,
    queued_at TEXT NOT NULL
);

-- Vendor portal ("SupplierHub") ----------------------------------------------
CREATE TABLE portal_accounts (
    username TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    customer TEXT NOT NULL
);
CREATE TABLE portal_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_ref TEXT NOT NULL UNIQUE,
    customer TEXT NOT NULL,
    vendor_name TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    total_paise INTEGER NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT NOT NULL
);

-- Acme Admin ----------------------------------------------------------------
CREATE TABLE admin_users (
    username TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('clerk', 'approver'))
);
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    address TEXT NOT NULL,
    gstin TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    bank_account TEXT NOT NULL,
    bank_ifsc TEXT NOT NULL
);
CREATE TABLE purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number TEXT NOT NULL UNIQUE,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    amount_paise INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_on TEXT NOT NULL
);
-- No uniqueness on (vendor, invoice_no): like many real systems, Admin accepts the
-- same invoice twice (D17). Preventing duplicates is the worker's job.
CREATE TABLE payables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    invoice_no TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    po_number TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('entered', 'pending_approval', 'approved')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE,
    full_name TEXT NOT NULL,
    work_email TEXT NOT NULL,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    start_date TEXT NOT NULL,
    manager_id INTEGER REFERENCES employees(id),
    status TEXT NOT NULL CHECK (status IN ('onboarding', 'active')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('it', 'facilities', 'finance', 'people')),
    requester TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE ticket_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE team_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    posted_at TEXT NOT NULL
);
-- Admin API: responses remembered per Idempotency-Key, like payment APIs do (D19).
CREATE TABLE api_idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Read models for the oracle (harness only) ---------------------------------
CREATE VIEW v_payables AS
SELECT p.id, p.ref, v.name AS vendor_name, v.code AS vendor_code, p.invoice_no,
       p.invoice_date, p.due_date, printf('%.2f', p.amount_paise / 100.0) AS amount,
       p.amount_paise, p.po_number, p.notes, p.status, p.created_by, p.created_at,
       p.approved_by, p.approved_at
FROM payables p JOIN vendors v ON v.id = p.vendor_id;

CREATE VIEW v_employees AS
SELECT e.id, e.ref, e.full_name, e.work_email, e.title, e.department, e.start_date,
       m.full_name AS manager_name, e.status, e.created_by, e.created_at
FROM employees e LEFT JOIN employees m ON m.id = e.manager_id;

CREATE VIEW v_sent_email AS
SELECT id, from_addr, to_addr, subject, body, sent_at
FROM mail_messages WHERE folder = 'sent';

CREATE VIEW v_outbound_email AS
SELECT o.id, o.to_addr, m.subject, m.body, o.queued_at
FROM outbound_mail o JOIN mail_messages m ON m.id = o.message_id;
