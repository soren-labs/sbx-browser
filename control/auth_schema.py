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
)
