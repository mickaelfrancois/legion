# legion

> Une stack d'agents Claude Code qui transforme une issue GitHub en pull request,
> via un pipeline discipliné où chaque étape est revue par un agent spécialisé qui
> **bloque** tant que ce n'est pas bon. Pensée pour le développement **.NET solo/indie**.

Inspirée de [gstack](https://github.com/garrytan/gstack). `legion` n'écrit (presque)
pas de code lui-même : c'est un **chef d'orchestre**. Il séquence des sous-agents
« gates » en lecture seule (qui rendent un verdict `accept` / `revise` / `reject`),
garde l'état de chaque tâche par repo, et **délègue** tout le travail .NET concret à
[`dotnet-claude-kit`](https://github.com/trossitec/dotnet-claude-kit).

```
THINK → PLAN → BUILD → REVIEW → TEST → DELIVER → REFLECT
start   architect builder reviewer test-eng  PR     retro
        (gate)   (producer)(gate)  (gate)
```

Une étape produit un artefact Markdown que la suivante consomme. Un build propre
enchaîne automatiquement les gates ; un `revise` arrête tout et renvoie corriger.

## Contenu du dépôt (monorepo)

| Dossier | Quoi |
|---|---|
| [`plugins/legion/`](plugins/legion) | Le **plugin Claude Code** : commandes (`/legion:battle`, `/legion:fleet`…), agents-gates, garde-fous (hooks), doctrine. |
| [`ui/legatus/`](ui/legatus) | **Legatus** — une UI web locale **read-only** (Blazor Server) pour suivre l'avancement des battles à travers tous tes repos. Optionnelle. |

## Démarrage rapide

**Prérequis** : [Claude Code](https://claude.com/claude-code), **Python 3**
(`python` — exécute les hooks), **.NET SDK**, la **GitHub CLI** (`gh auth login`),
et le plugin **`dotnet-claude-kit`** (fournit la MCP Roslyn + les skills délégués).

1. **Installer le plugin** dans Claude Code :
   ```
   /plugin marketplace add mickaelfrancois/legion
   /plugin install legion
   ```
2. **Ouvrir un repo .NET** et lancer une battle :
   ```
   /legion:battle start 42        # 42 = numéro d'issue GitHub (tirée via gh)
   /legion:battle build           # code la slice (toi en direct, ou --auto)
   /legion:battle review          # gates reviewer → test → security
   /legion:battle deliver         # branche, commit, push, PR (Closes #42)
   /legion:retro                  # rétro + apprentissage, ferme la battle
   ```
3. **(optionnel) Visualiser** avec Legatus : voir [`ui/legatus/README.md`](ui/legatus/README.md).

Guide complet, antisèche des commandes et garde-fous :
[`plugins/legion/README.md`](plugins/legion/README.md).
Conception & doctrine : [`plugins/legion/ARCHITECTURE.md`](plugins/legion/ARCHITECTURE.md).

## Comment ça marche en bref

- **Gates = sous-agents isolés** : démarrent en session vierge, lisent le livrable,
  rendent un verdict. Lecture seule — ils ne touchent jamais au code ni à l'état.
- **État local par repo** dans `.legion/battles/<id>/` (git-ignoré) ; la trace pérenne
  vit dans la PR + l'issue GitHub.
- **Vue multi-repo** : `/legion:fleet` agrège un index global (`~/.claude/legion/fleet.d/`)
  — c'est ce que lit Legatus.
- **Garde-fous exécutables** : `/legion:freeze` limite réellement le périmètre d'écriture
  (un hook bloque les éditions hors zone) ; `/legion:careful` avertit sur les commandes
  destructrices.

## Licence

[MIT](LICENSE).
