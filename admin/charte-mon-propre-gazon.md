# Charte visuelle — Mon Propre Gazon

Ce document et le fichier `mpg-tokens.css` qui l'accompagne décrivent l'habillage du site Mon Propre Gazon, extrait tel quel de son code plutôt que redécrit de mémoire. L'objectif est qu'un autre projet puisse reprendre la même identité sans avoir à ouvrir le site.

Le fichier CSS est du CSS ordinaire : aucune dépendance à Tailwind, à un préprocesseur ou à un composant React. Il s'utilise dans n'importe quelle page HTML, y compris une page produite par un script Python.

---

## 1. Le principe de fond

Une seule règle gouverne tout le reste : **aucun écran ne déclare de couleur**. Chaque écran demande un jeton nommé, et le jeton décide. C'est ce qui permet de changer une teinte à un seul endroit, de basculer en mode sombre sans repasser sur chaque page, et de garantir qu'une même notion porte partout la même couleur.

Un corollaire pratique : si une valeur doit être écrite deux fois, c'est qu'il manque un jeton.

---

## 2. La palette

### Bleu indigo : l'action

`#4054CC` est la couleur de tout ce sur quoi on peut cliquer, et de rien d'autre. Elle se décline en survol `#303F99`, en appui `#202A66`, en fond très clair `#F0F1FB` pour les zones actives.

`#CFD4F2` est réservé à **la ligne du visiteur** dans un classement. C'est le repère personnel : chacun se retrouve dans un tableau de 72 lignes grâce à cette teinte, doublée d'une étoile bleue. Ne pas la réemployer pour autre chose.

### Le dégradé du bandeau

`linear-gradient(90deg, #5D9E78 0%, #568A8B 45%, #507A9B 100%)` — vert pelouse à gauche, bleu ardoise à droite.

Les éléments posés dessus n'ont pas de fond de couleur propre : ils ont trois états faits de voiles. Repos sans fond, survol éclairci d'un voile blanc à 18 %, page courante en voile noir à 62 %. Le texte reste blanc dans les trois cas, avec un contraste mesuré d'au moins 4,9 pour 1 aux deux extrémités du dégradé. C'est la seule façon de garder un texte lisible sur un fond qui change de couleur d'un bout à l'autre.

### Les championnats

Chaque championnat a une teinte vive pour ses filets et ses pastilles, et un couple fond pâle + texte sombre pour les en-têtes de tableau. Les en-têtes sont figés au défilement, donc **leur fond doit rester opaque** : une transparence laisserait défiler les lignes en dessous.

| Championnat | Filet | En-tête (fond / texte) |
|---|---|---|
| Ligue 1 · Ligue Camembert | `#085FFF` | `#DFE8FF` / `#0B3F9C` |
| Premier League · Rosbeef | `#850291` | `#F2E0F4` / `#6A0274` |
| Liga · Liga Tapas | `#D4332D` | `#FBE1E0` / `#97231F` |
| Serie A · Lega Calzone | `#09EEFF` | `#D9F6F9` / `#076C76` |
| Ligue 2 · Ligue 2 EKT | `#00FFCE` | `#D8F8F0` / `#00705A` |
| Champions · Champignons | `#161E8D` | `#E0E2F2` / `#161E8D` |
| Super Classement | `#45C945` | `#DFF3DF` / `#1C5A1C` |
| Neutre | — | `#ECEEF2` / `#3C4350` |

Le cyan de la Serie A et le turquoise de la Ligue 2 sont trop clairs pour porter du texte : ils ont chacun une variante de texte dédiée, `#0A9AA6` et `#00A382`. Ne jamais écrire directement sur ces deux-là.

### Les états

Succès `#349734`, avertissement `#BA760A`, danger `#D4332D`, information `#161E8D`, chacun avec un fond très pâle assorti.

### Les notes de joueur

Sept degrés d'un vert foncé vers un rouge, en passant par un jaune olive : `#226422`, `#349734`, `#45C945`, `#BFB100`, `#807600`, `#BA760A`, `#D4332D`. L'échelle est volontairement asymétrique — le vert clair est plus lumineux que le jaune, ce qui creuse l'écart perçu entre « bon » et « moyen ».

---

## 3. Les seize tons de bonus

Le site distingue des dizaines de bonus. Plutôt que d'inventer une couleur par bonus, il définit **seize tons nommés** — acier, succès, feu, herbe, ambre, or, argent, bronze, danger, violet, rose, brun, sarcelle, indigo, délavé, neutre — et affecte chaque famille de bonus à un ton.

Chaque ton comporte cinq valeurs, ce qui permet de l'utiliser à trois échelles sans jamais retoucher une couleur à la main :

| Suffixe | Rôle | Opacité |
|---|---|---|
| `-bg` | fond plein d'une étiquette | 42 % (45 % en sombre) |
| `-surface` | fond d'une carte entière | 16 % (22 % en sombre) |
| `-edge` | bordure d'une carte | 40 % |
| `-fg` | texte lisible sur ces fonds | opaque |
| `-line` | bordure d'une étiquette | 70 % |

Tous les contrastes texte/fond ont été mesurés à la fois sur carte blanche et sur fond de page gris clair, et à la fois sur carte et sur fond en mode sombre : le minimum relevé est de 4,6 pour 1.

Deux tons méritent une note. **Délavé** et **neutre** sont deux gris différents parce que le gris sert à signaler un classement *honorifique*, sans points ; il fallait donc des gris rémunérés qui ne lui ressemblent pas. **Sarcelle** et **indigo** ont été ajoutés pour la même raison.

---

## 4. Les formes

Un seul rayon de coin dans tout le site : `0.5rem`, et sa variante réduite pour les étiquettes.

Les bordures sont fines — `0.5px` — partout : cartes, étiquettes, séparateurs de tableau. C'est ce qui donne au site son aspect posé plutôt que compartimenté.

Aucune police n'est importée. Le site utilise la pile système par défaut, ce qui est un choix par omission plutôt qu'une décision : si un autre projet veut une identité typographique propre, il n'écrase rien en la définissant.

---

## 5. Les tableaux

C'est la forme dominante du site, et elle obéit à quelques règles constantes.

L'en-tête est figé au défilement, avec un fond opaque de la teinte du championnat consulté. Les lignes alternent avec un très léger fond gris `#F5F6F6`. La ligne du visiteur porte le bleu `#CFD4F2` et une étoile.

Sur téléphone, le tableau ne défile pas horizontalement : il bascule en cartes empilées, une carte par participant, les compteurs groupés sur des lignes compactes plutôt qu'en une liste d'étiquettes.

À l'impression, la page passe en A4 paysage, l'en-tête de colonnes est répété en haut de chaque page, aucune ligne n'est coupée en deux, et tout ce qui porte la classe `mpg-no-print` disparaît.

---

## 6. Ce qui manque, et qu'il faut savoir avant de reprendre

Deux réserves, vérifiées dans le code plutôt que supposées.

**Le mode sombre est incomplet.** Sur les 137 jetons de la charte, 80 ont une valeur en mode sombre — ce sont les seize tons de bonus. Les 57 autres n'en ont pas : couleur de titre, textes atténués, bleu d'action, teintes de championnat, en-têtes de tableau, dégradé du bandeau, échelle de notes. En pratique le mode sombre n'est d'ailleurs jamais activé dans le site, aucun bouton ne le déclenche. Un projet qui en veut un devra définir ces 57 valeurs.

**Les teintes du Multi Boss ne sont pas dans ce fichier.** Elles ont été ajoutées au site après l'archive qui a servi à cette extraction. Le fichier CSS les signale en fin de document avec les noms attendus ; il faut recopier les valeurs réelles depuis le code courant plutôt que d'en inventer, sinon les deux projets divergeront dès la première retouche.

---

## 7. Fichiers

- `mpg-tokens.css` — tous les jetons et les composants, en CSS simple. À inclure tel quel, puis à consommer via `var(--mpg-…)` ou via les classes fournies.
- Ce document — le pourquoi de chaque choix, pour les décisions que le CSS seul ne raconte pas.
