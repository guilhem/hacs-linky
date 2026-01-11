# Linky Integration for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Intégration Home Assistant pour récupérer les données de consommation et production de votre compteur Linky via l'API [Conso](https://conso.boris.sh/).

## Fonctionnalités

- **Consommation journalière** - Énergie consommée en Wh
- **Consommation totale (7 jours)** - Total sur la semaine
- **Puissance actuelle** - Puissance moyenne sur 30 minutes en W
- **Puissance maximale** - Pic de puissance journalier en VA
- **Production journalière** - Pour les installations solaires (désactivé par défaut)
- **Puissance de production** - Puissance de production actuelle
- **📊 Historique complet** - Stockage jour par jour des données dans les statistiques Home Assistant
- **⚡ Energy Dashboard** - Intégration native avec le tableau de bord énergie

## Prérequis

1. Un compteur Linky installé
2. Un compte Enedis avec la collecte des données horaires activée
3. Un token d'authentification obtenu sur [conso.boris.sh](https://conso.boris.sh/)

## Installation

### Via HACS (recommandé)

1. Ouvrez HACS dans Home Assistant
2. Cliquez sur "Intégrations"
3. Cliquez sur les 3 points en haut à droite → "Dépôts personnalisés"
4. Ajoutez `https://github.com/guilhem/hacs-linky` avec la catégorie "Intégration"
5. Recherchez "Linky" et installez l'intégration
6. Redémarrez Home Assistant

### Installation manuelle

1. Copiez le dossier `custom_components/linky` dans votre dossier `config/custom_components/`
2. Redémarrez Home Assistant

## Configuration

1. Allez dans **Paramètres** → **Appareils et services** → **Ajouter une intégration**
2. Recherchez "Linky"
3. Entrez votre token obtenu sur [conso.boris.sh](https://conso.boris.sh/)
4. Si votre token donne accès à plusieurs compteurs, sélectionnez celui à configurer

### Importer l'historique

Pour importer des données historiques au-delà des 365 derniers jours automatiquement récupérés :

1. Allez dans **Outils de développement** → **Services**
2. Sélectionnez le service `linky.import_historical_data`
3. Entrez la date de début (ex: `2023-01-01`)
4. (Optionnel) Entrez la date de fin (par défaut: aujourd'hui)
5. Cliquez sur **Appeler le service**

Les données historiques seront importées et ajoutées aux statistiques existantes.

**Exemple YAML :**
```yaml
service: linky.import_historical_data
data:
  start_date: "2023-01-01"
  end_date: "2024-12-31"
```

📖 **Guide complet** : Consultez [IMPORT_HISTORY_GUIDE.md](IMPORT_HISTORY_GUIDE.md) pour des exemples détaillés, bonnes pratiques et résolution de problèmes.

## Entités créées

### Sensors

| Entité | Description | Unité |
|--------|-------------|-------|
| `sensor.linky_XXXXXX_daily_consumption` | Dernière consommation journalière | Wh |
| `sensor.linky_XXXXXX_total_consumption_week` | Total sur 7 jours | Wh |
| `sensor.linky_XXXXXX_current_power` | Puissance moyenne (30 min) | W |
| `sensor.linky_XXXXXX_max_power` | Puissance max du jour | VA |
| `sensor.linky_XXXXXX_daily_production` | Production journalière | Wh |
| `sensor.linky_XXXXXX_current_production_power` | Puissance de production | W |

### Statistiques (Energy Dashboard)

L'intégration enregistre automatiquement les données historiques jour par jour dans les statistiques Home Assistant :

- `linky:{PRM}_energy_consumption` - Historique de consommation journalière
- `linky:{PRM}_energy_production` - Historique de production journalière (si applicable)

Ces statistiques sont automatiquement disponibles dans le **Tableau de bord Énergie** de Home Assistant.

#### Configuration du Energy Dashboard

1. Allez dans **Paramètres** → **Tableaux de bord** → **Énergie**
2. Dans **Consommation du réseau** : sélectionnez `Linky {PRM} consumption`
3. (Optionnel) Dans **Retour au réseau** : sélectionnez `Linky {PRM} production`

Pour plus de détails sur l'implémentation des statistiques, consultez [STATISTICS_IMPLEMENTATION.md](STATISTICS_IMPLEMENTATION.md).

## Différences avec ha-linky

| Critère | hacs-linky | ha-linky |
| --- | --- | --- |
| Type | Intégration native | Add-on |
| Installation | HACS / manuel | Supervisor |
| Compatible HAOS | Oui | Oui |
| Compatible Docker | Oui | Non (nécessite Supervisor) |
| Historique | Illimité (statistiques) | 1 an |
| Energy Dashboard | Oui (natif) | Non |
| Calcul des coûts | Non | Oui |

## Dépannage

### Token invalide

Vérifiez que votre token est toujours valide sur [conso.boris.sh](https://conso.boris.sh/). Les tokens peuvent expirer.

### Pas de données

Les données Linky sont disponibles avec un délai de 24 à 48 heures. La puissance instantanée (load curve) peut avoir un délai supplémentaire.

## Licence

MIT
