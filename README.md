# legion

> Une stack d'agents Claude Code qui transforme une issue GitHub en pull request,
> via un pipeline discipliné où chaque étape est revue par un agent spécialisé qui
> **bloque** tant que ce n'est pas bon. Pensée pour le développement **.NET solo/indie**.

Inspirée de [gstack](https://github.com/garrytan/gstack). `legion` n'écrit (presque)
pas de code lui-même : c'est un **chef d'orchestre**. Il séquence des sous-agents
« gates » en lecture seule sur le code (qui rendent un verdict `accept` / `revise` / `reject`),
garde l'état de chaque tâche par repo, et s'appuie sur le **SDK .NET seul** pour le
travail .NET concret : aucun plugin tiers n'est requis.

```
THINK → PLAN → BUILD → LINT → REVIEW → TEST → DELIVER → REFLECT
start   architect builder lint   reviewer test-eng  PR    retro
        (gate)   (producer)(gate) (gate)   (gate)
```

Une étape produit un artefact Markdown que la suivante consomme. La gate **LINT**
(formatage .NET) est **.NET-only** : elle se retire sans bloquer sur un repo non-.NET.

**Run autonome (défaut).** Tu n'arbitres **qu'une fois** : à l'approbation du plan.
Sur ton OK, le pipeline enchaîne seul `build → lint → review → test → deliver` et
**ouvre la PR** sans nouvelle confirmation. Il ne rend la main que sur blocage —
`reject`, boucle qui ne converge pas, ou filet de livraison (cf. taxonomie d'escalade).
Un `revise` déclenche une boucle d'auto-correction bornée. Force le pas-à-pas avec
`/legion:battle start <issue> --step`.

## Contenu du dépôt (monorepo)

| Dossier | Quoi |
|---|---|
| [`plugins/legion/`](plugins/legion) | Le **plugin Claude Code** : commandes (`/legion:battle`, `/legion:fleet`…), agents-gates, garde-fous (hooks), doctrine. |
| [`ui/legatus/`](ui/legatus) | **Legatus** — une UI web locale **read-only** (Blazor Server) pour suivre l'avancement des battles à travers tous tes repos. Optionnelle. |

## Démarrage rapide

**Prérequis** : [Claude Code](https://claude.com/claude-code), **Python 3**
(`python` — exécute les hooks), **.NET SDK**, la **GitHub CLI** (`gh auth login`),
(aucun plugin tiers requis).
*Optionnel* : un serveur LSP C# (ex. `dotnet/skills`) enrichit la gate `reviewer` ;
prérequis SDK .NET 10, NuGet au premier lancement, serveur en préversion.

1. **Installer le plugin** dans Claude Code :
   ```
   /plugin marketplace add mickaelfrancois/legion
   /plugin install legion
   ```
2. **Ouvrir un repo .NET** et lancer une battle :
   ```
   /legion:battle start 42        # 42 = numéro d'issue GitHub (tirée via gh)
   /legion:battle build           # code la slice (toi en direct, ou --auto)
   /legion:battle review          # gates lint → reviewer → test → security
   /legion:battle deliver         # branche, commit, push, PR (Closes #42)
   /legion:retro                  # rétro + apprentissage, ferme la battle
   ```
   En mode autonome (défaut), `build` → `review` → `deliver` **s'enchaînent seuls**
   après l'approbation du plan : ces commandes ne sont à lancer à la main qu'en
   `--step`, ou pour reprendre après un blocage. Les retours de revue sur la PR
   se traitent avec `/legion:battle address` (répétable).
3. **(optionnel) Visualiser** avec Legatus, l'UI web locale read-only :
   `/legion:legatus` la lance depuis n'importe quel repo. Détails :
   [`ui/legatus/README.md`](ui/legatus/README.md).

Guide complet, antisèche des commandes et garde-fous :
[`plugins/legion/README.md`](plugins/legion/README.md).
Conception & doctrine : [`plugins/legion/ARCHITECTURE.md`](plugins/legion/ARCHITECTURE.md).

## Comment ça marche en bref

- **Gates = sous-agents isolés** : démarrent en session vierge, lisent le livrable,
  rendent un verdict. Lecture seule sur le code — ils écrivent seulement leur artefact, jamais le code.
- **État local par repo** dans `.legion/battles/<id>/` (git-ignoré) ; la trace pérenne
  vit dans la PR + l'issue GitHub.
- **Vue multi-repo** : `/legion:fleet` agrège un index global (`~/.claude/legion/fleet.d/`)
  — c'est ce que lit Legatus.
- **Garde-fous exécutables** : `/legion:freeze` (ou `/legion:guard`, périmètre déduit
  du plan) limite réellement la zone d'écriture — un hook bloque toute édition hors
  zone, builder compris ; `/legion:careful` avertit sur les commandes destructrices.
- **Opportunités hors-scope → issues GitHub** : une amélioration ou anomalie repérée
  pendant une battle mais **hors du périmètre** de la feature devient, au REFLECT, une
  **issue de suivi** sur le repo (dédoublonnée) — pour qu'une prochaine battle la traite.

## Licence

[MIT](LICENSE).

## Note de migration

Les versions précédentes de `legion` demandaient d'installer le plugin
[`dotnet-claude-kit`](https://github.com/trossitec/dotnet-claude-kit). Ce n'est plus
nécessaire :

- le kit n'est plus requis ; tu peux le désinstaller sans effet sur `legion` ;
- les skills `scaffold` et `modern-csharp` sont abandonnés ;
- les règles de revue, de sécurité, d'architecture et de test sont internalisées dans
  les agents ;
- le serveur LSP C# reste optionnel : sans lui, la gate `reviewer` passe sur un repli tracé.
