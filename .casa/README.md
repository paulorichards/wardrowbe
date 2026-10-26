# La rama `casa`

Fork de [Anyesh/wardrowbe](https://github.com/Anyesh/wardrowbe) para el NAS de
la casa. Los parches se proponen upstream; `casa` existe para correrlos antes
de que los acepten.

| Rama | Qué es | Quién la mueve |
|---|---|---|
| `main` | espejo exacto de `upstream/main` | `casa-sync`, cada 6 h |
| `casa` (default) | último release de upstream + nuestros parches + este CI | `casa-sync` al salir un release; nosotros al agregar un parche |
| `fix/*`, `feat/*` | un parche, nacido de `main`, con su PR upstream | nosotros |

`.casa/base` dice sobre qué release de upstream está `casa`.

## El ciclo de un parche

1. Rama desde `main`, commit con conventional commits y su test, PR a upstream.
2. El mismo commit, `cherry-pick` a `casa` → `casa-build` publica imagen.
3. Cuando upstream lo libera, el rebase de `casa-sync` lo descarta solo. Si el
   mantenedor lo cambió al aceptarlo, el rebase choca: `casa-sync` abre un
   issue y `casa` queda como estaba.

## Imágenes

`ghcr.io/paulorichards/wardrowbe:{backend,frontend}-X.Y.Z-casa.N` — X.Y.Z es
el release de upstream debajo, N la corrida de `casa-build`. Sólo amd64. El
despliegue es a mano desde `nas-gitops` (Renovate abre el PR; las migraciones
de alembic se corren a mano).

## Workflows de upstream

Están **desactivados** en este repo: sin eso, cada push del espejo a `main`
correría release-please y abriría PRs de release acá, y los bots de bienvenida
comentarían nuestros issues. `casa-sync` apaga en cada vuelta todo workflow
activo que no se llame `casa-*`, así que uno nuevo de upstream no alcanza a
molestar más de 6 horas.

## Secret

`CASA_SYNC_TOKEN`: token fine-grained, sólo este repo, **Contents** y
**Workflows** en lectura/escritura. Respaldo en Infisical (`nas-gitops`).
