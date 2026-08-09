"""
Unattended version of fetch_espn_news.py for GitHub Actions -- same service-account Drive
auth pattern as the original scheduled script, but pulling from ESPN instead of
FantasyPros. Notably SIMPLER than the FantasyPros version: no API key needed at all, since
ESPN's endpoint requires no authentication.

Reads DRIVE_FOLDER_ID and GOOGLE_SERVICE_ACCOUNT_JSON from environment variables (GitHub
Secrets) -- no FP_API_KEY needed for this script specifically anymore.
"""
import pandas as pd
import numpy as np
import requests
import re
import os
import io
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SERVICE_ACCOUNT_INFO = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

creds = service_account.Credentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=['https://www.googleapis.com/auth/drive'])
drive_service = build('drive', 'v3', credentials=creds)

def find_file_id(filename):
    query = f"'{DRIVE_FOLDER_ID}' in parents and name = '{filename}' and trashed = false"
    results = drive_service.files().list(q=query, fields='files(id, name)').execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def download_csv(filename):
    file_id = find_file_id(filename)
    if file_id is None:
        return None
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return pd.read_csv(buffer)

def upload_csv(df, filename):
    buffer = io.BytesIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    media = MediaIoBaseUpload(buffer, mimetype='text/csv', resumable=True)
    file_id = find_file_id(filename)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        metadata = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
        drive_service.files().create(body=metadata, media_body=media, fields='id').execute()

def normalize_name(n):
    if pd.isna(n):
        return None
    n = str(n).lower()
    n = re.sub(r"[.']", '', n)
    n = re.sub(r'\b(jr|sr|ii|iii|iv)\b', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n

TEAM_MAP = {'JAX': 'JAC', 'WSH': 'WAS', 'LA': 'LAR', 'OAK': 'LV', 'SD': 'LAC', 'STL': 'LAR'}
def harmonize_team(t):
    if pd.isna(t):
        return t
    return TEAM_MAP.get(str(t).upper(), str(t).upper())

INJURY_KEYWORDS = ['injury', 'injured', 'acl', 'mcl', 'concussion', 'questionable', 'doubtful',
                    'out for', 'placed on ir', 'injured reserve', 'hurt', 'surgery', 'hamstring',
                    'ankle', 'knee', 'shoulder', 'groin', 'hip', 'foot', 'wrist', 'ruled out']
def looks_injury_related(headline, description):
    text = f"{headline or ''} {description or ''}".lower()
    return any(kw in text for kw in INJURY_KEYWORDS)

board = download_csv('draft_board_2026_final.csv')
if board is None:
    raise SystemExit("draft_board_2026_final.csv not found in the Drive folder -- check DRIVE_FOLDER_ID and sharing.")
board['name_norm'] = board['name'].apply(normalize_name)
board['team_h'] = board['team'].apply(harmonize_team)

resp = requests.get('https://site.api.espn.com/apis/site/v2/sports/football/nfl/news',
                     params={'limit': 50})
resp.raise_for_status()
data = resp.json()
articles = data.get('articles', [])
print(f"Loaded {len(articles)} articles from ESPN")

matched_rows = []
for article in articles:
    categories = article.get('categories', [])
    athlete_names = [c.get('description') for c in categories if c.get('type') == 'athlete' and c.get('description')]
    team_abbrevs = [harmonize_team(c.get('team', {}).get('abbreviation')) for c in categories if c.get('type') == 'team']
    article_team = team_abbrevs[0] if team_abbrevs else None

    for athlete_name in athlete_names:
        athlete_norm = normalize_name(athlete_name)
        candidates = board[board['name_norm'] == athlete_norm]
        if len(candidates) > 1 and article_team:
            team_matched = candidates[candidates['team_h'] == article_team]
            candidates = team_matched if len(team_matched) > 0 else candidates.head(1)
        for _, board_row in candidates.iterrows():
            matched_rows.append({
                'id': article.get('id'),
                'name': board_row['name'],
                'headline': article.get('headline'),
                'description': article.get('description'),
                'published': article.get('published'),
                'is_injury_related': looks_injury_related(article.get('headline'), article.get('description')),
                'link': article.get('links', {}).get('web', {}).get('href'),
            })

matched_news = pd.DataFrame(matched_rows)
print(f"{len(matched_news)} article-player matches")

existing = download_csv('espn_news.csv')
if existing is not None:
    combined = pd.concat([existing, matched_news], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=['id', 'name'], keep='last')
    print(f"Combined with existing history: {len(existing)} old + {len(matched_news)} new -> "
          f"{len(combined)} after dedup (removed {before - len(combined)} duplicates)")
    matched_news = combined
else:
    print("No existing news file found in Drive -- starting fresh history.")

upload_csv(matched_news, 'espn_news.csv')
print(f"Saved espn_news.csv -- {len(matched_news)} items total (accumulated history)")
