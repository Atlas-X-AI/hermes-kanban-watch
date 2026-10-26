-- Claude inbox schema copied from ping.it tests/inbox_schema.sql at origin/main.
-- Only loaded into disposable test databases. Never opens the live inbox.
CREATE TABLE molle_input_holds (session TEXT PRIMARY KEY);
CREATE TABLE molle_input_hold_rows (message_id INTEGER PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL, target TEXT NOT NULL, message_type TEXT NOT NULL,
    payload TEXT NOT NULL, read INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    notified INTEGER DEFAULT 0, dead_target INTEGER DEFAULT 0,
    notify_attempts INTEGER DEFAULT 0, last_notify_error TEXT, last_notify_attempt_at REAL,
    notify_target TEXT, archived INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
    dedupe_key TEXT, expires_at TEXT, target_pane TEXT, backend_type TEXT,
    obligation INTEGER, withdrawn_by TEXT, withdrawn_at TEXT, withdrawal_reason TEXT,
    consumed INTEGER DEFAULT 0, consumed_at TEXT, consumption_evidence TEXT,
    notify_session_id TEXT, notify_window_id TEXT, notify_pane_id TEXT, consumed_by TEXT
);
CREATE TRIGGER molle_input_hold_insert AFTER INSERT ON messages
    WHEN NEW.notified = 0 AND EXISTS (
        SELECT 1 FROM molle_input_holds WHERE NEW.target = session OR instr(NEW.target, session || ':') = 1
    ) BEGIN
        INSERT OR IGNORE INTO molle_input_hold_rows SELECT NEW.id, session FROM molle_input_holds
            WHERE NEW.target = session OR instr(NEW.target, session || ':') = 1;
        UPDATE messages SET notified = 2 WHERE id = NEW.id;
    END;
CREATE TRIGGER molle_input_hold_update AFTER UPDATE OF notified ON messages
    WHEN NEW.notified = 0 AND EXISTS (
        SELECT 1 FROM molle_input_holds WHERE NEW.target = session OR instr(NEW.target, session || ':') = 1
    ) BEGIN
        INSERT OR IGNORE INTO molle_input_hold_rows SELECT NEW.id, session FROM molle_input_holds
            WHERE NEW.target = session OR instr(NEW.target, session || ':') = 1;
        UPDATE messages SET notified = 2 WHERE id = NEW.id;
    END;
CREATE TRIGGER molle_input_hold_delivered AFTER UPDATE OF notified ON messages
    WHEN NEW.notified = 1 BEGIN
        DELETE FROM molle_input_hold_rows WHERE message_id = NEW.id;
    END;
