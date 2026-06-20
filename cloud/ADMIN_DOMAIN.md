# Weasley Admin Domain

The browser admin UI uses a shared high-entropy secret only on the login form.
Successful login creates a signed, 12-hour `HttpOnly`, `Secure`,
`SameSite=Strict` cookie. Query-string authentication is not supported.

API clients can continue to send `WEASLEY_API_KEY` in the `x-api-key` header.

## One-time setup

1. Generate a dedicated session signing key and store it in 1Password:

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

2. Request an ACM certificate in `us-east-1`:

   ```bash
   aws acm request-certificate \
     --region us-east-1 \
     --domain-name weasley-admin.doughty.org \
     --validation-method DNS
   ```

3. Add the ACM validation CNAME to the `doughty.org` DNS zone at GoDaddy and
   wait for the certificate status to become `ISSUED`.

4. Configure the deployment environment:

   ```dotenv
   WEASLEY_SESSION_SIGNING_KEY=<value from 1Password>
   WEASLEY_ADMIN_DOMAIN_NAME=weasley-admin.doughty.org
   WEASLEY_ADMIN_CERTIFICATE_ARN=<issued ACM certificate ARN>
   WEASLEY_DISABLE_EXECUTE_API_ENDPOINT=false
   ```

5. Run `cloud/deploy.sh`. Read the `AdminDomainTarget` stack output and create
   this DNS record at GoDaddy:

   ```text
   weasley-admin CNAME <AdminDomainTarget>
   ```

## Cutover

Verify login, dashboard display, and place create/update/delete operations at
`https://weasley-admin.doughty.org`. Then set:

```dotenv
WEASLEY_DISABLE_EXECUTE_API_ENDPOINT=true
```

Deploy again and verify that the original `execute-api` URL returns `403`.
Rotate `WEASLEY_API_KEY` after removing old bookmarked URLs. Rotating
`WEASLEY_SESSION_SIGNING_KEY` invalidates all active browser sessions.

## Rollback

Set `WEASLEY_DISABLE_EXECUTE_API_ENDPOINT=false` and redeploy to restore the
default API Gateway hostname. Header authentication remains available during
the cutover; query-string authentication should not be restored.
