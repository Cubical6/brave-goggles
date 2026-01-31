# Brave Goggles

Custom [Brave Goggles](https://search.brave.com/help/goggles) for filtering search results towards curated developer documentation.

## Goggles

### Dev Documentation (`dev-docs.goggle`)

Whitelist goggle that restricts Brave Search results to curated developer documentation sites. Includes:

- **Framework docs** — Laravel, Angular, Vue, Svelte, FastAPI, Django, CakePHP
- **Language references** — PHP, JavaScript/TypeScript, Python
- **Infrastructure** — Docker, AWS, Redis, PostgreSQL, Caddy, GitHub Actions
- **General resources** — Stack Overflow, DevDocs, MDN
- **GitHub org boosting** — Auto-generated per-org boost rules based on project dependencies, tiered by usage frequency

## Auto-generated GitHub rules

The `resolve-github-orgs.py` script scans local project directories for dependency manifests (`composer.json`, `package.json`, `requirements.txt`, `pyproject.toml`), resolves each package to its GitHub organization via registry APIs, and injects tiered boost rules into the goggle:

| Packages per org | Boost level |
|------------------|-------------|
| 5+               | `$boost=5`  |
| 3–4              | `$boost=4`  |
| 1–2              | `$boost=3`  |

```bash
# Default: scan ~/bitbucket and ~/github
python3 resolve-github-orgs.py

# Custom directories
python3 resolve-github-orgs.py ~/projects ~/work

# Preview without writing
python3 resolve-github-orgs.py --dry-run

# Force re-resolve all packages
python3 resolve-github-orgs.py --clear-cache
```

## Usage

Use the raw goggle URL in Brave Search:

```
https://raw.githubusercontent.com/Cubical6/brave-goggles/main/dev-docs.goggle
```
