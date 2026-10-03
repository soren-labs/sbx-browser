"""Append-only auth migrations, shared by PostgreSQL and local SQLite.

Auth tables are separate from sandbox sessions/provider accounts. Never edit an
applied migration: add a new numbered migration instead.
"""

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE user_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            revoked_at TEXT
        )""",
        "CREATE INDEX user_sessions_user_idx ON user_sessions(user_id)",
        """CREATE TABLE oauth_accounts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_subject TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(provider, provider_subject)
        )""",
        "CREATE INDEX oauth_accounts_user_idx ON oauth_accounts(user_id)",
        """CREATE TABLE api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            key_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            scopes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at DOUBLE PRECISION,
            revoked_at TEXT
        )""",
        "CREATE INDEX api_keys_user_idx ON api_keys(user_id)",
    ),
    (
        """CREATE TABLE password_credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            email_verified_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE email_verification_challenges (
            email TEXT PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            code_hash TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            resend_after DOUBLE PRECISION NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            registration_hash TEXT UNIQUE,
            registration_expires_at DOUBLE PRECISION,
            consumed_at TEXT
        )""",
        """CREATE TABLE auth_rate_limits (
            bucket_hash TEXT PRIMARY KEY,
            window_start DOUBLE PRECISION NOT NULL,
            attempts INTEGER NOT NULL
        )""",
    ),
    (
        """CREATE TABLE control_records (
            namespace TEXT NOT NULL,
            id TEXT NOT NULL,
            owner TEXT,
            payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(namespace, id)
        )""",
        "CREATE INDEX control_records_owner_idx ON control_records(namespace, owner)",
    ),
)
