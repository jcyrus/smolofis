//! Samba AD authentication over certificate-verified LDAPS.
use ldap3::{Ldap, LdapConnAsync, LdapConnSettings, Scope, SearchEntry, SearchOptions};
use rustls::pki_types::{pem::PemObject, CertificateDer};
use serde::Deserialize;
use std::{future::Future, pin::Pin, sync::Arc, time::Duration};
use zeroize::Zeroizing;

type Outcome<'a, T> = Pin<Box<dyn Future<Output = Result<T, ()>> + Send + 'a>>;

#[derive(Clone, PartialEq, Eq)]
pub struct Identity(pub [u8; 16]);

pub trait Directory: Send + Sync {
    fn authenticate<'a>(&'a self, username: &'a str, password: &'a str) -> Outcome<'a, Identity>;
    fn authorized<'a>(&'a self, identity: &'a Identity) -> Outcome<'a, bool>;
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DirectoryConfig {
    pub url: String,
    pub realm: String,
    pub base_dn: String,
    pub admin_group_dn: String,
    pub ca_file: std::path::PathBuf,
    pub reader_dn: String,
    pub reader_password_file: std::path::PathBuf,
}

pub struct AdDirectory {
    config: DirectoryConfig,
    tls: Arc<rustls::ClientConfig>,
    reader_password: Zeroizing<String>,
}

impl AdDirectory {
    pub fn new(config: DirectoryConfig) -> anyhow::Result<Self> {
        let url = url::Url::parse(&config.url)?;
        anyhow::ensure!(
            url.scheme() == "ldaps"
                && url.host_str().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
                && matches!(url.path(), "" | "/"),
            "directory requires a plain ldaps endpoint"
        );
        anyhow::ensure!(
            !config.realm.is_empty()
                && config
                    .realm
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'.' || c == b'-'),
            "invalid directory realm"
        );
        anyhow::ensure!(
            !config.base_dn.is_empty()
                && !config.admin_group_dn.is_empty()
                && !config.reader_dn.is_empty(),
            "directory DNs must be explicit"
        );
        let pem = std::fs::read(&config.ca_file)?;
        let mut roots = rustls::RootCertStore::empty();
        for cert in CertificateDer::pem_slice_iter(&pem) {
            roots.add(cert?)?;
        }
        anyhow::ensure!(
            !roots.is_empty(),
            "directory CA file contains no certificates"
        );
        let tls = rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()?
        .with_root_certificates(roots)
        .with_no_client_auth();
        let mut reader_password =
            Zeroizing::new(std::fs::read_to_string(&config.reader_password_file)?);
        let length = reader_password.trim_end_matches(['\r', '\n']).len();
        reader_password.truncate(length);
        anyhow::ensure!(
            !reader_password.is_empty(),
            "directory reader password must not be empty"
        );
        Ok(Self {
            config,
            tls: Arc::new(tls),
            reader_password,
        })
    }

    async fn connect(&self) -> Result<Ldap, ()> {
        let settings = LdapConnSettings::new()
            .set_config(self.tls.clone())
            .set_conn_timeout(Duration::from_secs(3));
        let (connection, ldap) = LdapConnAsync::with_settings(settings, &self.config.url)
            .await
            .map_err(|_| ())?;
        // Dropping the last LDAP handle closes the driver, including cancellation.
        tokio::spawn(async move {
            let _ = connection.drive().await;
        });
        Ok(ldap)
    }

    fn filter(&self, selector: &str) -> String {
        format!("(&(objectClass=user){selector}(!(userAccountControl:1.2.840.113556.1.4.803:=2))(memberOf:1.2.840.113556.1.4.1941:={}))", ldap3::ldap_escape(&self.config.admin_group_dn))
    }

    async fn find(&self, ldap: &mut Ldap, selector: &str) -> Result<Option<Identity>, ()> {
        let (entries, _) = ldap
            .with_search_options(SearchOptions::new().sizelimit(2).timelimit(3))
            .search(
                &self.config.base_dn,
                Scope::Subtree,
                &self.filter(selector),
                vec!["objectGUID"],
            )
            .await
            .map_err(|_| ())?
            .success()
            .map_err(|_| ())?;
        if entries.is_empty() {
            return Ok(None);
        }
        if entries.len() != 1 {
            return Err(());
        }
        let entry = SearchEntry::construct(entries.into_iter().next().ok_or(())?);
        // ldap3 may decode any binary attribute as UTF-8 when its bytes happen
        // to be valid text. Accept either representation without changing bytes.
        let bytes: Vec<Vec<u8>> = entry
            .bin_attrs
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case("objectGUID"))
            .map(|(_, values)| values.clone())
            .or_else(|| {
                entry
                    .attrs
                    .iter()
                    .find(|(key, _)| key.eq_ignore_ascii_case("objectGUID"))
                    .map(|(_, values)| {
                        values
                            .iter()
                            .map(|value| value.as_bytes().to_vec())
                            .collect()
                    })
            })
            .ok_or(())?;
        if bytes.len() != 1 {
            return Err(());
        }
        Ok(Some(Identity(
            bytes[0].as_slice().try_into().map_err(|_| ())?,
        )))
    }
}

pub fn valid_username(username: &str) -> bool {
    !username.is_empty()
        && username.len() <= 64
        && username
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

impl Directory for AdDirectory {
    fn authenticate<'a>(&'a self, username: &'a str, password: &'a str) -> Outcome<'a, Identity> {
        Box::pin(async move {
            if !valid_username(username) || password.is_empty() || password.len() > 1024 {
                return Err(());
            }
            let mut ldap = self.connect().await?;
            let principal = format!("{}@{}", username, self.config.realm);
            ldap.simple_bind(&principal, password)
                .await
                .map_err(|_| ())?
                .success()
                .map_err(|_| ())?;
            let identity = self
                .find(
                    &mut ldap,
                    &format!("(sAMAccountName={})", ldap3::ldap_escape(username)),
                )
                .await?
                .ok_or(())?;
            let _ = ldap.unbind().await;
            Ok(identity)
        })
    }

    fn authorized<'a>(&'a self, identity: &'a Identity) -> Outcome<'a, bool> {
        Box::pin(async move {
            let mut ldap = self.connect().await?;
            ldap.simple_bind(&self.config.reader_dn, &self.reader_password)
                .await
                .map_err(|_| ())?
                .success()
                .map_err(|_| ())?;
            let guid = identity
                .0
                .iter()
                .map(|byte| format!("\\{byte:02x}"))
                .collect::<String>();
            let found = self
                .find(&mut ldap, &format!("(objectGUID={guid})"))
                .await?;
            let _ = ldap.unbind().await;
            Ok(found.as_ref() == Some(identity))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn usernames_cannot_change_bind_realm_or_search() {
        for username in [
            "",
            "a@evil.test",
            "LAB\\alice",
            "*)(objectClass=*)",
            "a\0b",
            "a b",
        ] {
            assert!(!valid_username(username));
        }
        for username in ["alice", "office-admin", "a.b_2"] {
            assert!(valid_username(username));
        }
    }
}
