#!/usr/bin/env python3
"""Télécharge les données publiques LNH vers data/ pour GitHub Pages, sans dépendances."""
import json, os, pathlib, time, urllib.request, urllib.error, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEST = ROOT / 'data'
BASE = 'https://api-web.nhle.com/v1'
DEST.mkdir(exist_ok=True)
HEADERS = {'User-Agent': 'CH-LIVE/3.0 (GitHub Pages public dashboard)', 'Accept':'application/json'}

def download(path, attempts=3):
    url = BASE + path
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == attempts-1: raise RuntimeError(f'{path}: {exc}') from exc
            time.sleep(1.5 * (attempt+1))

def save(path, value):
    dest = DEST / (path.lstrip('/')+'.json')
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp = dest.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    temp.replace(dest)

def fetch_and_save(path):
    value=download(path)
    # L'horodatage est dans le JSON : GitHub Actions réinitialise les dates des fichiers.
    if path.startswith('/player/') and isinstance(value, dict):
        value['_collected_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save(path,value)
    return value

def cache_age_seconds(path):
    """Retourne l'âge réel d'une fiche, indépendant du checkout GitHub."""
    target=DEST/(path.lstrip('/')+'.json')
    if not target.exists(): return float('inf')
    try:
        obj=json.loads(target.read_text(encoding='utf-8'))
        stamp=obj.get('_collected_at')
        if not stamp: return float('inf')  # ancienne fiche : rafraîchir au moins une fois
        dt=datetime.datetime.fromisoformat(stamp.replace('Z','+00:00'))
        return max(0,(datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds())
    except (ValueError,TypeError,OSError,AttributeError):
        return float('inf')

def season_totals_changed(player, kind):
    """Compare le résumé du club à la fiche mise en cache, sans requête réseau."""
    pid=player.get('playerId')
    if not pid: return False
    file=DEST/'player'/str(pid)/'landing.json'
    if not file.exists(): return True
    try:
        landing=json.loads(file.read_text(encoding='utf-8'))
        featured=landing.get('featuredStats',{}).get('regularSeason',{}).get('subSeason',{})
        if not featured: return False  # ne pas créer une boucle de requêtes sur données absentes
        keys=('gamesPlayed','wins','losses','shutouts') if kind=='goalie' else ('gamesPlayed','goals','assists','points')
        for key in keys:
            if player.get(key) is not None and featured.get(key) is not None:
                if str(player[key]) != str(featured[key]): return True
    except (ValueError,OSError,TypeError,AttributeError):
        return True
    return False

def refresh_player_batch(paths, max_items=120, workers=6, label='fiche',
                         threshold_hours=6, priority_paths=()):
    """Priorité aux statistiques modifiées, puis aux fichiers les plus anciens.
    Les éléments non échus ne sont pas redemandés; limite stricte par collecte.
    """
    unique=list(dict.fromkeys(paths))
    priority=set(priority_paths)
    candidates=[]
    for rel in unique:
        age=cache_age_seconds(rel)
        if rel in priority or age >= threshold_hours*3600:
            candidates.append((rel not in priority, -age, rel))
    candidates.sort()
    selected=[rel for _,_,rel in candidates[:max_items]]
    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs={pool.submit(fetch_and_save,rel):rel for rel in selected}
        for job in as_completed(jobs):
            try: job.result()
            except Exception as exc:
                failures.append(jobs[job])
                print('AVERTISSEMENT',label,jobs[job],exc,flush=True)
    print(f'{label}: {len(selected)-len(failures)}/{len(selected)} actualisées; '
          f'{len(priority & set(selected))} prioritaires; {len(candidates)} éligibles.',flush=True)
    return len(selected),failures

results={}
for key,path in [('schedule','/club-schedule-season/MTL/now'),('stats','/club-stats/MTL/now'),('standings','/standings/now')]:
    try:
        results[key]=fetch_and_save(path)
        print('OK',path,flush=True)
    except Exception as exc:
        print('ERREUR',exc,flush=True)

if 'schedule' not in results or 'stats' not in results:
    raise SystemExit('Impossible de récupérer les données essentielles; anciens fichiers conservés.')

def localized_name(x):
    return x.get('default','') if isinstance(x,dict) else str(x or '')

players = results['stats'].get('skaters',[])
season = next((g.get('season') for g in results['schedule'].get('games',[]) if g.get('season')),None)
if not season:
    now=datetime.datetime.now(datetime.timezone.utc)
    start=now.year if now.month>=7 else now.year-1
    season=int(f'{start}{start+1}')

paths=[]
for p in players:
    pid=p.get('playerId')
    if pid:
        paths.extend([f'/player/{pid}/landing',f'/player/{pid}/game-log/{season}/2'])
for g in results['schedule'].get('games',[]):
    if g.get('gameState') in ('LIVE','CRIT') and g.get('id'):
        paths.extend([f'/gamecenter/{g["id"]}/landing',f'/gamecenter/{g["id"]}/boxscore'])

failed=[]
with ThreadPoolExecutor(max_workers=5) as pool:
    futures={pool.submit(fetch_and_save,p):p for p in paths}
    for future in as_completed(futures):
        try: future.result()
        except Exception as exc:
            failed.append(str(exc))
            print('AVERTISSEMENT',exc,flush=True)

# Horodatage de fin enregistré après toutes les collectes.
print(f'Terminé : {len(paths)-len(failed)}/{len(paths)} fiches/détails chargés; {len(players)} joueurs.')

# Archive complète des dates de la saison et rafraîchissement des matchs récents.
from zoneinfo import ZoneInfo
local_day=datetime.datetime.now(ZoneInfo('America/Toronto')).date()
start_year=int(str(season)[:4]);start=datetime.date(start_year,9,1);end=datetime.date(start_year+1,7,1)
# Toutes les dates : les fichiers existants sont conservés. Les journées à venir
# et les 3 derniers jours sont renouvelés à chaque exécution.
days=[start+datetime.timedelta(days=i) for i in range((end-start).days+1)]
paths_to_fetch=[]
for day in days:
    file=DEST/'league'/'score'/f'{day.isoformat()}.json'
    if not file.exists() or abs((day-local_day).days)<=3:
        paths_to_fetch.append(day)

def fetch_score(day):
    value=download('/score/'+day.isoformat())
    value['cached_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    save('/league/score/'+day.isoformat(),value)
    return day

score_errors=[]
with ThreadPoolExecutor(max_workers=6) as pool:
    futures={pool.submit(fetch_score,d):d for d in paths_to_fetch}
    for future in as_completed(futures):
        try:future.result()
        except Exception as exc:
            day=futures[future]
            score_errors.append(day.isoformat())
            print('AVERTISSEMENT pointages LNH',day,exc,flush=True)
save('/league/dates',{'start':start.isoformat(),'end':end.isoformat(),
    'dates':[d.isoformat() for d in days],
    'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'errors':score_errors})
print(f'Pointages : {len(paths_to_fetch)-len(score_errors)}/{len(paths_to_fetch)} journées téléchargées.')

# Classement des meilleurs pointeurs de toute la LNH.
try:
    leaders=download('/skater-stats-leaders/current?categories=points&limit=30')
    save('/skater-stats-leaders/current',leaders)
    print('Meilleurs pointeurs LNH : collecte réussie.',flush=True)
except Exception as exc:
    print('AVERTISSEMENT meilleurs pointeurs :',exc,flush=True)


# Fiches des derniers matchs de tous les joueurs, y compris B/A/PTS.
last_games={}
for p in players:
    pid=p.get('playerId')
    if not pid: continue
    f=DEST/'player'/str(pid)/'game-log'/str(season)/'2.json'
    if not f.exists(): continue
    try:
        entries=json.loads(f.read_text(encoding='utf-8')).get('gameLog',[])
        entries=sorted((x for x in entries if x.get('gameDate')),key=lambda x:x['gameDate'])
        if entries:
            g=entries[-1]
            last_games[str(pid)]=f"{g.get('goals',0)}-{g.get('assists',0)}-{g.get('points',0)}"
    except (ValueError,OSError) as exc: print('AVERTISSEMENT fiche joueur',pid,exc)
save('/players-last-games',{'season':season,'players':last_games})

# Détails des matchs : tous les matchs terminés du CH et les rencontres
# de toute la LNH des 14 derniers jours, en plus des matchs en cours.
all_score_files=list((DEST/'league'/'score').glob('*.json')) if (DEST/'league'/'score').exists() else []
match_ids=set()
for g in results['schedule'].get('games',[]):
    if g.get('id') and g.get('gameState') in ('FINAL','OFF','LIVE','CRIT'):
        match_ids.add(int(g['id']))
for file in all_score_files:
    try:
        day=datetime.date.fromisoformat(file.stem)
        if not (local_day-datetime.timedelta(days=14)<=day<=local_day): continue
        for g in json.loads(file.read_text(encoding='utf-8')).get('games',[]):
            if g.get('id') and g.get('gameState') in ('FINAL','OFF','LIVE','CRIT'):
                match_ids.add(int(g['id']))
    except (ValueError,OSError): continue
match_paths=[]
for gid in sorted(match_ids):
    for kind in ('landing','boxscore'):
        rel=f'/gamecenter/{gid}/{kind}'
        target=DEST/(rel.lstrip('/')+'.json')
        if not target.exists() or gid in [int(g['id']) for g in results['schedule'].get('games',[]) if g.get('id') and g.get('gameState') in ('LIVE','CRIT')]:
            match_paths.append(rel)
match_errors=[]
with ThreadPoolExecutor(max_workers=6) as pool:
    futures={pool.submit(fetch_and_save,path):path for path in match_paths}
    for future in as_completed(futures):
        try:future.result()
        except Exception as exc:
            match_errors.append(str(exc))
            print('AVERTISSEMENT détail match',exc,flush=True)
# Horodatage de la collecte terminé, après toutes les sections.
metadata={'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'season':season,'players':len(players),'last_game_players':len(last_games),
          'match_details_requested':len(match_paths),'match_details_failed':len(match_errors),
          'score_dates_failed':len(score_errors)}
save('/metadata',metadata)
print('Collecte terminée',metadata,flush=True)

# V9 : effectifs et statistiques des 32 équipes.
team_codes=sorted({(x.get('teamAbbrev') or {}).get('default','') for x in results.get('standings',{}).get('standings',[])})
team_codes=[c for c in team_codes if c]
# Conserver les 32 clubs même si le classement officiel est temporairement vide.
known_teams=['ANA','BOS','BUF','CAR','CBJ','CGY','CHI','COL','DAL','DET','EDM','FLA','LAK','MIN','MTL','NJD','NSH','NYI','NYR','OTT','PHI','PIT','SEA','SJS','STL','TBL','TOR','UTA','VAN','VGK','WPG','WSH']
team_codes=sorted(set(team_codes)|set(known_teams))
if not team_codes:
    team_codes=['ANA','BOS','BUF','CAR','CBJ','CGY','CHI','COL','DAL','DET','EDM','FLA','LAK','MIN','MTL','NJD','NSH','NYI','NYR','OTT','PHI','PIT','SEA','SJS','STL','TBL','TOR','UTA','VAN','VGK','WPG','WSH']
all_players={}
all_goalies={}
collected_teams=set()
failed_teams=[]
def fetch_team(code):
    return code,download('/club-stats/'+code+'/now')
with ThreadPoolExecutor(max_workers=5) as pool:
    jobs={pool.submit(fetch_team,c):c for c in team_codes}
    for job in as_completed(jobs):
        code=jobs[job]
        try:
            _,team=job.result()
            for p in team.get('skaters',[]):
                pid=p.get('playerId')
                if not pid: continue
                item=dict(p)
                item['name']=localized_name(p.get('firstName'))+' '+localized_name(p.get('lastName'))
                item['teamAbbrev']=code
                all_players[str(pid)]=item
            # Les gardiens sont inclus dans la même réponse /club-stats/{code}/now.
            # Les enregistrer explicitement dans la liste globale du championnat.
            for p in team.get('goalies',[]):
                pid=p.get('playerId')
                if not pid: continue
                item=dict(p)
                item['name']=(localized_name(p.get('firstName'))+' '+localized_name(p.get('lastName'))).strip()
                item['teamAbbrev']=code
                all_goalies[str(pid)]=item
            collected_teams.add(code)
        except Exception as exc:
            failed_teams.append(code)
            print('AVERTISSEMENT équipe',code,exc,flush=True)
# Les gardiens sont publiés dans un fichier unique lu directement par NHL LIVE.
# Conserver les anciens gardiens des équipes en échec pour éviter de vider le tableau.
previous_goalies_file=DEST/'league'/'goalies.json'
if previous_goalies_file.exists():
    try:
        previous=json.loads(previous_goalies_file.read_text(encoding='utf-8'))
        for old in previous.get('goalies',[]):
            code=str(old.get('teamAbbrev') or '')
            pid=old.get('playerId')
            if pid and code not in collected_teams and str(pid) not in all_goalies:
                all_goalies[str(pid)]=old
    except (OSError,ValueError,TypeError) as exc:
        print('AVERTISSEMENT anciens gardiens',exc,flush=True)
if all_goalies:
    save('/league/goalies',{'season':season,'goalies':list(all_goalies.values()),
        'teams_collected':len(collected_teams),'failed_teams':failed_teams,
        'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print('Gardiens LNH :',len(all_goalies),'gardiens,',len(collected_teams),
          'équipes collectées,',len(failed_teams),'échecs.',flush=True)
else:
    print('AVERTISSEMENT : aucun gardien reçu, ancien fichier conservé.',flush=True)

if all_players:
    save('/league/skaters',{'season':season,'players':list(all_players.values()),'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print('Statistiques LNH :',len(all_players),'joueurs.',flush=True)

# V10.4 : rafraîchir les journaux des joueurs ET gardiens par rotation, y compris ceux déjà créés.
# Le quota limite les requêtes par exécution sans bloquer indéfiniment les mêmes joueurs.
ordered=sorted(all_players.values(),key=lambda p:p.get('points') or 0,reverse=True)
# Inclure aussi les gardiens : leurs fiches utilisent les mêmes endpoints.
profile_players=list({str(p['playerId']):p for p in [*ordered,*all_goalies.values()] if p.get('playerId')}.values())
log_paths=[f'/player/{p["playerId"]}/game-log/{season}/2' for p in profile_players]
log_requested,log_failures=refresh_player_batch(log_paths,120,6,'Journaux joueurs LNH',threshold_hours=2)

# Détails des rencontres du jour, toutes équipes : éviter les 404 après
# la première collecte et réactualiser les rencontres en cours.
for offset in (-1,0,1):
    day=local_day+datetime.timedelta(days=offset)
    try:
        score=download('/score/'+day.isoformat())
        save('/league/score/'+day.isoformat(),score)
        active=[g for g in score.get('games',[]) if g.get('id') and g.get('gameState') in ('FINAL','OFF','LIVE','CRIT')]
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs={pool.submit(fetch_and_save,f'/gamecenter/{g["id"]}/{kind}'):(g['id'],kind) for g in active for kind in ('landing','boxscore')}
            for job in as_completed(jobs):
                try:job.result()
                except Exception as exc:print('AVERTISSEMENT détail récent',exc,flush=True)
    except Exception as exc:print('AVERTISSEMENT journée récente',day,exc,flush=True)
# Horodatage final, seulement après la collecte de toutes les équipes.
meta_file=DEST/'metadata.json'
meta=json.loads(meta_file.read_text(encoding='utf-8')) if meta_file.exists() else {}
meta.update({'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'league_players':len(all_players),'version':'9'})
save('/metadata',meta)
# Actualiser également les fiches existantes (pas uniquement les nouvelles).
landing_paths=[f'/player/{p["playerId"]}/landing' for p in profile_players]
# Les statistiques du club sont déjà téléchargées : comparer avant d'appeler l'API.
# Les joueurs dont les totaux ont changé passent en priorité pour mettre à jour Carrière.
changed_landing_paths=[]
for p in profile_players:
    kind='goalie' if str(p['playerId']) in all_goalies else 'skater'
    if season_totals_changed(p,kind):
        changed_landing_paths.append(f'/player/{p["playerId"]}/landing')
landing_requested,landing_failures=refresh_player_batch(
    landing_paths,120,6,'Fiches avancées LNH',threshold_hours=6,
    priority_paths=changed_landing_paths)
print('Fiches avec statistiques modifiées :',len(changed_landing_paths),flush=True)
# Fusionner les valeurs avancées disponibles dans le tableau global.
for p in all_players.values():
    f=DEST/'player'/str(p['playerId'])/'landing.json'
    if not f.exists():continue
    try:
        landing=json.loads(f.read_text(encoding='utf-8'))
        stats=landing.get('featuredStats',{}).get('regularSeason',{}).get('subSeason',{})
        p['avgTimeOnIce']=stats.get('avgToi') or stats.get('avgTimeOnIce') or p.get('avgTimeOnIce')
        p['powerPlayGoals']=stats.get('powerPlayGoals',p.get('powerPlayGoals'))
        p['faceoffWinPctg']=stats.get('faceoffWinningPctg',p.get('faceoffWinPctg'))
    except (OSError,ValueError):pass
if all_players:
    save('/league/skaters',{'season':season,'players':list(all_players.values()),'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
print('Fiches avancées :',landing_requested,'demandées.',flush=True)

# V10 : classement complet depuis les fiches statistiques des 32 clubs.
# Contrairement à /skater-stats-leaders, cette source contient MJ, B, A et PTS.
if all_players:
    ranked=sorted(all_players.values(),key=lambda x:(int(x.get('points') or 0),int(x.get('goals') or 0),int(x.get('assists') or 0)),reverse=True)
    save('/league/top-scorers',{'season':season,'players':[{'playerId':p.get('playerId'),'name':p.get('name'),'teamAbbrev':p.get('teamAbbrev'),'gamesPlayed':p.get('gamesPlayed'),'goals':p.get('goals'),'assists':p.get('assists'),'points':p.get('points')} for p in ranked[:100]],'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
meta_file=DEST/'metadata.json'
meta=json.loads(meta_file.read_text(encoding='utf-8')) if meta_file.exists() else {}
meta.update({'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'season':season,'league_players':len(all_players),'league_teams':len(set(p.get('teamAbbrev') for p in all_players.values())),'league_goalies':len(all_goalies),'goalie_teams_collected':len(collected_teams),'player_logs_requested':log_requested,'player_logs_failed':len(log_failures),'player_landings_requested':landing_requested,'player_landings_failed':len(landing_failures),'failed_teams':failed_teams,'profile_players':len(profile_players),'profiles_changed':len(changed_landing_paths),'version':'10.5'})
save('/metadata',meta)
print('V10 :',len(all_players),'joueurs LNH,',meta['league_teams'],'équipes, classement complet généré.',flush=True)
