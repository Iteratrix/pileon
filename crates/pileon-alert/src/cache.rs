//! A [`Fetch`] over the lab's response cache, for replaying what was graded.
//!
//! The lab keys responses by URL and indexes posts one by one
//! (`lab/src/pileon_lab/fetch.py`). Reading the same tables by the same keys
//! gives the Rust pipeline exactly the responses the Python one saw. Nothing
//! is fetched: a miss is a reply with status 0, as the lab's offline mode
//! returns.

use std::cell::Cell;
use std::path::Path;

use anyhow::Context;
use pileon_core::appview::{Fetch, FetchError, Reply, Request};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Map, Value};

/// Read-only access to a lab cache file.
pub struct CacheFetch {
    conn: Connection,
    misses: Cell<u64>,
}

impl CacheFetch {
    /// Opens `path` read-only.
    ///
    /// # Errors
    ///
    /// Fails when the file cannot be opened as SQLite.
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .with_context(|| format!("opening {}", path.display()))?;
        Ok(Self {
            conn,
            misses: Cell::new(0),
        })
    }

    /// Returns how many requests were not in the cache.
    pub fn misses(&self) -> u64 {
        self.misses.get()
    }

    fn lookup(&self, url: &str) -> Result<Reply, FetchError> {
        let row: Option<(u16, Option<String>)> = self
            .conn
            .query_row(
                "SELECT status, body FROM responses WHERE url = ?1",
                [url],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(|err| failed(url, &err))?;
        let Some((status, body)) = row else {
            self.misses.set(self.misses.get() + 1);
            return Ok(Reply {
                status: 0,
                body: None,
            });
        };
        Ok(Reply {
            status,
            body: body.as_deref().and_then(object),
        })
    }

    fn posts(&self, uris: &[String]) -> Result<Vec<Map<String, Value>>, FetchError> {
        let mut out = Vec::new();
        let mut statement = self
            .conn
            .prepare_cached("SELECT body FROM posts WHERE uri = ?1")
            .map_err(|err| failed("posts", &err))?;
        for uri in uris {
            let found: Option<Option<String>> = statement
                .query_row([uri], |row| row.get(0))
                .optional()
                .map_err(|err| failed(uri, &err))?;
            match found {
                Some(Some(body)) => out.extend(object(&body)),
                Some(None) => {}
                None => self.misses.set(self.misses.get() + 1),
            }
        }
        Ok(out)
    }
}

fn object(text: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str(text) {
        Ok(Value::Object(map)) => Some(map),
        Ok(_) | Err(_) => None,
    }
}

fn failed(url: &str, err: &rusqlite::Error) -> FetchError {
    FetchError {
        url: url.to_owned(),
        reason: err.to_string(),
    }
}

impl Fetch for CacheFetch {
    fn get(&self, request: &Request) -> impl Future<Output = Result<Reply, FetchError>> {
        let reply = self.lookup(&request.url());
        async move { reply }
    }

    fn get_posts(
        &self,
        uris: &[String],
    ) -> impl Future<Output = Result<Vec<Map<String, Value>>, FetchError>> {
        let posts = self.posts(uris);
        async move { posts }
    }
}
