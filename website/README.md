# pursers.app static site

The deployable site is `index.html` plus `style.css`. It has no build step,
runtime JavaScript, analytics, trackers, external fonts, or remote assets.

Preview from the repository root:

```sh
python3 -m http.server 8000 --directory website
```

Then open `http://127.0.0.1:8000/`.

The empty `og:image` meta value is deliberate. When the social card exists,
publish it as `docs/showcase/pursers-og.png` at the site origin and replace the
empty value with `https://pursers.app/docs/showcase/pursers-og.png`. Do not
activate that URL before the asset resolves.

## Deployment choices

The operator should choose exactly one deployment path. None is activated by
this directory.

### 1. GitHub Pages

1. Copy [`deploy/pages.yml`](deploy/pages.yml) to
   `.github/workflows/pages.yml` in an operator-owned change.
2. In repository **Settings > Pages**, set the source to **GitHub Actions**.
3. Set the custom domain to `pursers.app`, wait for the DNS check, then enable
   HTTPS enforcement.

The draft follows GitHub's current static Pages flow: checkout, configure
Pages, upload `website/` as the Pages artifact, then deploy it. There is no
site build command.

Use this exact DNS set for GitHub Pages. Remove conflicting apex `A`, `AAAA`,
`ALIAS`, or `ANAME` records first; do not add a wildcard record.

| Type | Name | Value |
| --- | --- | --- |
| `A` | `@` | `185.199.108.153` |
| `A` | `@` | `185.199.109.153` |
| `A` | `@` | `185.199.110.153` |
| `A` | `@` | `185.199.111.153` |
| `AAAA` | `@` | `2606:50c0:8000::153` |
| `AAAA` | `@` | `2606:50c0:8001::153` |
| `AAAA` | `@` | `2606:50c0:8002::153` |
| `AAAA` | `@` | `2606:50c0:8003::153` |
| `CNAME` | `www` | `swisspra.github.io.` |

Source: [GitHub's custom-domain documentation](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site).

### 2. Cloudflare Pages

Create a Git-integrated Pages project with these values:

| Setting | Value |
| --- | --- |
| Project name | `pursers` |
| Production branch | `main` |
| Root directory | `/` |
| Build command | *(leave empty)* |
| Build output directory | `website` |

The `pursers.app` zone must use Cloudflare nameservers and be in the same
account as the Pages project. In **Workers & Pages > pursers > Custom domains**,
add `pursers.app` before touching DNS; Cloudflare creates the apex CNAME. Add
`www.pursers.app` as a second custom domain before its record too. Manually
creating either CNAME first can produce a 522 response.

For a Pages project whose assigned production hostname is exactly
`pursers.pages.dev`, the resulting DNS set is:

| Type | Name | Target | Proxy |
| --- | --- | --- | --- |
| `CNAME` | `@` | `pursers.pages.dev` | Proxied |
| `CNAME` | `www` | `pursers.pages.dev` | Proxied |

If Cloudflare assigns a different `*.pages.dev` hostname because `pursers` is
unavailable, stop and replace both targets with the exact hostname shown in
the Pages dashboard; do not deploy the table above unchanged.

Source: [Cloudflare Pages custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/).

### 3. Plain static host

Upload the contents of `website/` to the host's document root. Configure
`pursers.app` as the canonical domain, serve `index.html` at `/`, serve
`style.css` as `text/css`, and redirect HTTP to HTTPS.

DNS cannot be truthfully pre-filled until the operator chooses a host and that
host issues its origin address or canonical hostname. Use the host's exact
`A`/`AAAA` values or apex-capable `ALIAS`/`ANAME`/flattened `CNAME`; never copy
the GitHub or Cloudflare values above to an unrelated provider.

## Local validation

From the repository root:

```sh
python3 -c 'from html.parser import HTMLParser; p = HTMLParser(); p.feed(open("website/index.html", encoding="utf-8").read()); p.close(); print("HTML parser: OK")'
python3 tools/leak_scan.py
git diff --check
```

Also parse every `href` in `index.html` and `README.md`: fragment targets must
exist, relative paths must resolve inside `website/`, and each HTTPS URL must
return a successful or redirecting response.
