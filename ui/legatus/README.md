# Legatus

UI locale **read-only** de suivi des battles [legion](../../plugins/legion).

Lit l'index global sharded `fleet.d/` (un `<sha1>.json` par battle) et les artefacts Markdown
des battles (`spec.md`, `plan.md`, `build-report.md`, gates, `pr-body.md`, `retro.md`)
directement sur le filesystem. N'écrit **jamais** dans `.legion/` ni dans `fleet.d/`
(voir le contrat d'intégration `plugins/legion/docs/ui-integration.md`).

## Fonctionnalités
- Liste filtrable des battles (recherche repo/titre/ticket, `clôturées` masquées par défaut, `bloquées` en tête).
- Frise des phases du pipeline (THINK → PLAN → BUILD → LINT → REVIEW → TEST → SECURITY → DELIVER → ADDRESS → REFLECT) avec statut et verdicts de gate ; le slot SECURITY reste *pending* quand la gate n'est pas requise, le slot ADDRESS tant que la PR n'a pas reçu de commentaire de revue.
- Détail d'une battle : rendu des artefacts Markdown dans l'ordre de lecture.
- **Rafraîchissement live** : le dossier `fleet.d/` est surveillé (debounce 300 ms) ; l'UI se met à jour à chaque transition de phase, sans rechargement.
- Gestion des entrées orphelines (repo déplacé/supprimé) sans plantage.

## Stack
- .NET 10 / Blazor Server (Interactive Server)
- MudBlazor (UI)
- Markdig (rendu Markdown GFM)

## Prérequis
- .NET SDK 10

## Lancer

Lancement recommandé, depuis n'importe quel repo (Linux, WSL2, macOS, Windows) :

```text
/legion:legatus
```

En direct, avec le lanceur multi-OS (`python` sous Windows) :

```bash
python3 plugins/legion/scripts/legatus.py [--dry-run] [--port N]
```

Le lanceur démarre Legatus **détaché** (il survit à la session), journalise dans
`~/.claude/legion/legatus.log`, ouvre le navigateur et affiche un objet JSON (`state`, `url`,
`command`, ...). `--port N` fait écouter Legatus sur N ; `--dry-run` montre la commande
(`command`) sans rien lancer.

Sans le lanceur, depuis la racine de `ui/legatus` (tous OS) :

```bash
dotnet run --project src/presentation
```

L'app démarre sur http://localhost:5021 (profil `http` par défaut) et ouvre le navigateur.
Elle n'écoute que sur la boucle locale.

Pour forcer HTTPS (port 7177) :

```bash
dotnet run --project src/presentation --launch-profile https
```

> Sans variable `LEGION_FLEET`, l'app lit l'index réel sous
> `~/.claude/legion` (`%USERPROFILE%\.claude\legion` sous Windows). Pour une démo sans données réelles, génère d'abord
> un index de test (section suivante).

## Données de test (seed)

> `tools/Seed-Fleet.ps1` est un outil **Windows / PowerShell**.

`tools/Seed-Fleet.ps1` projette des battles dans un index `fleet.d/` **isolé**
(jamais le vrai index). Il peut générer des battles synthétiques variées (actives / bloquées /
clôturées à différentes phases, avec tokens, skills et `usage.jsonl`), ou projeter de
vrais dossiers `.legion`.

**Workflow complet, à exécuter dans la même session PowerShell** :

```powershell
# 1. Générer 10 battles synthétiques (en purgeant l'index de test existant)
./tools/Seed-Fleet.ps1 -Synthetic -Count 10 -Clean

# 2. Pointer l'app sur cet index de test
$env:LEGION_FLEET = (Resolve-Path ./testdata/fleet-base).Path

# 3. Lancer
dotnet run --project src/presentation
```

L'étape 2 ne vaut que pour la session PowerShell courante ; relance-la dans toute nouvelle
fenêtre. Le rafraîchissement live fonctionne aussi sur l'index de test : relancer le seed
pendant que l'app tourne met l'UI à jour sans rechargement.

Pour projeter de **vrais** dossiers `.legion` au lieu de données synthétiques, passe leurs
chemins en argument (racine de repo ou dossier `.legion`) :

```powershell
./tools/Seed-Fleet.ps1 -Clean C:\repos\mon-repo C:\repos\autre-repo
```

| Paramètre | Rôle | Défaut |
|---|---|---|
| `BattlePath` (positionnel) | un ou plusieurs repos / dossiers `.legion` à projeter | — |
| `-Synthetic` | génère aussi des battles synthétiques | désactivé |
| `-Count` | nombre de battles synthétiques (avec `-Synthetic`) | `10` |
| `-OutputBase` | dossier de base ; les shards vont dans `<OutputBase>/fleet.d/` | `testdata/fleet-base` |
| `-Clean` | purge les shards existants avant d'écrire | désactivé |

## Configuration
| Variable | Rôle | Défaut |
|---|---|---|
| `LEGION_FLEET` | dossier de base dont on dérive `…/fleet.d/` | `~/.claude/legion` (`%USERPROFILE%\.claude\legion` sous Windows) |
