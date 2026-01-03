from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
from supabase import create_client, Client
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from datetime import timedelta, datetime
from config import Config
import urllib.parse
import requests
import base64

app = Flask(__name__)
app.config.from_object(Config)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# Initialize Supabase
supabase: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)

# Initialize Spotify (client credentials flow - no user login needed)
spotify = spotipy.Spotify(
    client_credentials_manager=SpotifyClientCredentials(
        client_id=Config.SPOTIFY_CLIENT_ID,
        client_secret=Config.SPOTIFY_CLIENT_SECRET
    )
)

# Spotify OAuth scopes
SPOTIFY_SCOPES = 'playlist-read-private playlist-modify-public playlist-modify-private user-read-private'


def get_spotify_auth_url():
    """Generate Spotify authorization URL."""
    params = {
        'client_id': Config.SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': Config.SPOTIFY_REDIRECT_URI,
        'scope': SPOTIFY_SCOPES,
        'show_dialog': 'true'
    }
    return 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode(params)


def exchange_code_for_tokens(code):
    """Exchange authorization code for access and refresh tokens."""
    auth_header = base64.b64encode(
        f"{Config.SPOTIFY_CLIENT_ID}:{Config.SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()

    response = requests.post(
        'https://accounts.spotify.com/api/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': Config.SPOTIFY_REDIRECT_URI
        },
        headers={
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    )

    if response.status_code == 200:
        return response.json()
    return None


def refresh_spotify_token(refresh_token):
    """Refresh an expired Spotify access token."""
    auth_header = base64.b64encode(
        f"{Config.SPOTIFY_CLIENT_ID}:{Config.SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()

    response = requests.post(
        'https://accounts.spotify.com/api/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        },
        headers={
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    )

    if response.status_code == 200:
        return response.json()
    return None


def get_user_spotify_client(user_id):
    """Get a Spotify client for a user with valid access token."""
    try:
        profile = supabase.table('profiles').select(
            'spotify_access_token, spotify_refresh_token, spotify_token_expires'
        ).eq('id', user_id).single().execute()

        if not profile.data or not profile.data.get('spotify_access_token'):
            return None

        # Check if token is expired
        expires_at = profile.data.get('spotify_token_expires')
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            if datetime.now(expires_dt.tzinfo) >= expires_dt:
                # Token expired, refresh it
                refresh_token = profile.data.get('spotify_refresh_token')
                if not refresh_token:
                    return None

                token_data = refresh_spotify_token(refresh_token)
                if not token_data:
                    return None

                # Update tokens in database
                new_expires = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])
                update_data = {
                    'spotify_access_token': token_data['access_token'],
                    'spotify_token_expires': new_expires.isoformat()
                }
                # Spotify may return a new refresh token
                if 'refresh_token' in token_data:
                    update_data['spotify_refresh_token'] = token_data['refresh_token']

                supabase.table('profiles').update(update_data).eq('id', user_id).execute()

                return spotipy.Spotify(auth=token_data['access_token'])

        return spotipy.Spotify(auth=profile.data['spotify_access_token'])
    except Exception as e:
        print(f"Error getting user Spotify client: {e}")
        return None


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ============== AUTH ROUTES ==============

@app.route('/')
def index():
    """Landing page - show popular public lists and user search."""
    # Get popular public lists (most recent for now)
    result = supabase.table('lists').select('*, profiles(username)').eq('is_public', True).order('created_at', desc=True).limit(12).execute()
    public_lists = result.data if result.data else []

    # Get item counts, preview images, and like counts for each list
    for lst in public_lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(4).execute()
        lst['preview_images'] = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0
        # Get like count
        try:
            like_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', lst['id']).execute()
            lst['like_count'] = like_result.count if like_result.count else 0
        except Exception:
            lst['like_count'] = 0

    return render_template('index.html', public_lists=public_lists)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """User registration."""
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        username = request.form.get('username')

        try:
            # Create auth user
            auth_response = supabase.auth.sign_up({
                'email': email,
                'password': password
            })

            if auth_response.user:
                # Create profile
                supabase.table('profiles').insert({
                    'id': auth_response.user.id,
                    'username': username,
                    'email': email
                }).execute()

                flash('Account created! Please log in.', 'success')
                return redirect(url_for('login'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login."""
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember') == 'on'

        try:
            auth_response = supabase.auth.sign_in_with_password({
                'email': email,
                'password': password
            })

            if auth_response.user:
                # Get profile
                profile = supabase.table('profiles').select('*').eq('id', auth_response.user.id).single().execute()

                # Set session to permanent if Remember Me is checked
                if remember:
                    session.permanent = True

                session['user'] = {
                    'id': auth_response.user.id,
                    'email': auth_response.user.email,
                    'username': profile.data.get('username') if profile.data else email.split('@')[0]
                }
                session['access_token'] = auth_response.session.access_token

                return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'Login failed: {str(e)}', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Log out user."""
    session.clear()
    return redirect(url_for('index'))


# ============== DASHBOARD & LISTS ==============

@app.route('/dashboard')
@login_required
def dashboard():
    """User's personal dashboard showing their lists."""
    user_id = session['user']['id']

    result = supabase.table('lists').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
    my_lists = result.data if result.data else []

    # Get item counts and preview images for each list
    for lst in my_lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(4).execute()
        lst['item_count'] = len(items_result.data) if items_result.data else 0
        lst['preview_images'] = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]

        # Get total count
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0

    return render_template('dashboard.html', lists=my_lists)


@app.route('/list/new', methods=['GET', 'POST'])
@login_required
def create_list():
    """Create a new list."""
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description', '')
        is_ranked = request.form.get('is_ranked') == 'on'
        is_public = request.form.get('is_public') == 'on'

        try:
            result = supabase.table('lists').insert({
                'user_id': session['user']['id'],
                'title': title,
                'description': description,
                'is_ranked': is_ranked,
                'is_public': is_public
            }).execute()

            if result.data:
                return redirect(url_for('edit_list', list_id=result.data[0]['id']))
            else:
                flash('Failed to create list', 'error')
        except Exception as e:
            flash(f'Error creating list: {str(e)}', 'error')

    return render_template('create_list.html')


@app.route('/list/<list_id>')
def view_list(list_id):
    """View a list (public or own)."""
    # Get list
    list_result = supabase.table('lists').select('*, profiles(username)').eq('id', list_id).single().execute()

    if not list_result.data:
        flash('List not found', 'error')
        return redirect(url_for('index'))

    lst = list_result.data

    # Check access
    is_owner = 'user' in session and session['user']['id'] == lst['user_id']
    if not lst['is_public'] and not is_owner:
        flash('This list is private', 'error')
        return redirect(url_for('index'))

    # Get items
    items_result = supabase.table('list_items').select('*').eq('list_id', list_id).order('position').execute()
    items = items_result.data if items_result.data else []

    return render_template('view_list.html', list=lst, items=items, is_owner=is_owner)


@app.route('/list/<list_id>/edit')
@login_required
def edit_list(list_id):
    """Edit a list (add/remove/reorder songs)."""
    # Verify ownership
    list_result = supabase.table('lists').select('*').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()

    if not list_result.data:
        flash('List not found or access denied', 'error')
        return redirect(url_for('dashboard'))

    lst = list_result.data

    # Get items
    items_result = supabase.table('list_items').select('*').eq('list_id', list_id).order('position').execute()
    items = items_result.data if items_result.data else []

    return render_template('edit_list.html', list=lst, items=items)


# ============== API ROUTES ==============

@app.route('/api/spotify/search')
@login_required
def spotify_search():
    """Search Spotify for tracks."""
    query = request.args.get('q', '')
    if not query or len(query) < 2:
        return jsonify({'tracks': []})

    try:
        results = spotify.search(q=query, type='track', limit=10)
        tracks = []
        for item in results['tracks']['items']:
            tracks.append({
                'id': item['id'],
                'name': item['name'],
                'artist': ', '.join(a['name'] for a in item['artists']),
                'album': item['album']['name'],
                'album_art': item['album']['images'][0]['url'] if item['album']['images'] else None
            })
        return jsonify({'tracks': tracks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/list/<list_id>/add', methods=['POST'])
@login_required
def add_to_list(list_id):
    """Add a track to a list."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    data = request.json

    # Get current max position
    pos_result = supabase.table('list_items').select('position').eq('list_id', list_id).order('position', desc=True).limit(1).execute()
    next_position = (pos_result.data[0]['position'] + 1) if pos_result.data else 1

    # Insert item
    result = supabase.table('list_items').insert({
        'list_id': list_id,
        'position': next_position,
        'spotify_track_id': data.get('track_id'),
        'track_name': data.get('track_name'),
        'artist_name': data.get('artist_name'),
        'album_name': data.get('album_name'),
        'album_art_url': data.get('album_art_url')
    }).execute()

    return jsonify({'success': True, 'item': result.data[0] if result.data else None})


@app.route('/api/list/<list_id>/remove/<item_id>', methods=['DELETE'])
@login_required
def remove_from_list(list_id, item_id):
    """Remove a track from a list."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    supabase.table('list_items').delete().eq('id', item_id).execute()
    return jsonify({'success': True})


@app.route('/api/list/<list_id>/update/<item_id>', methods=['POST'])
@login_required
def update_list_item(list_id, item_id):
    """Update a track in a list (swap for a different version)."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    data = request.json

    # Update the item - filter by both id and list_id for safety
    try:
        result = supabase.table('list_items').update({
            'spotify_track_id': data.get('track_id'),
            'track_name': data.get('track_name'),
            'artist_name': data.get('artist_name'),
            'album_name': data.get('album_name'),
            'album_art_url': data.get('album_art_url')
        }).eq('id', item_id).eq('list_id', list_id).execute()

        return jsonify({'success': True, 'updated': len(result.data) if result.data else 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/list/<list_id>/delete', methods=['DELETE'])
@login_required
def delete_list(list_id):
    """Delete a list and all its items."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    # Delete all items first
    supabase.table('list_items').delete().eq('list_id', list_id).execute()

    # Delete the list
    supabase.table('lists').delete().eq('id', list_id).execute()

    return jsonify({'success': True})


@app.route('/api/list/<list_id>/settings', methods=['POST'])
@login_required
def update_list_settings(list_id):
    """Update list settings (title, description, public, ranked)."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    data = request.json
    update_data = {}

    if 'title' in data:
        update_data['title'] = data['title']
    if 'description' in data:
        update_data['description'] = data['description']
    if 'is_public' in data:
        update_data['is_public'] = data['is_public']
    if 'is_ranked' in data:
        update_data['is_ranked'] = data['is_ranked']

    if update_data:
        supabase.table('lists').update(update_data).eq('id', list_id).execute()

    return jsonify({'success': True})


@app.route('/api/list/<list_id>/duplicate', methods=['POST'])
@login_required
def duplicate_list(list_id):
    """Duplicate a list (own list or public list)."""
    # Get the source list
    list_result = supabase.table('lists').select('*').eq('id', list_id).single().execute()
    if not list_result.data:
        return jsonify({'error': 'List not found'}), 404

    source_list = list_result.data

    # Check access - must be owner or list must be public
    is_owner = session['user']['id'] == source_list['user_id']
    if not source_list['is_public'] and not is_owner:
        return jsonify({'error': 'Access denied'}), 403

    # Create new list
    new_list = supabase.table('lists').insert({
        'user_id': session['user']['id'],
        'title': source_list['title'] + ' (Copy)',
        'description': source_list['description'],
        'is_ranked': source_list['is_ranked'],
        'is_public': False  # Copies start as private
    }).execute()

    if not new_list.data:
        return jsonify({'error': 'Failed to create list'}), 500

    new_list_id = new_list.data[0]['id']

    # Copy all items
    items_result = supabase.table('list_items').select('*').eq('list_id', list_id).order('position').execute()
    if items_result.data:
        for item in items_result.data:
            supabase.table('list_items').insert({
                'list_id': new_list_id,
                'position': item['position'],
                'spotify_track_id': item['spotify_track_id'],
                'track_name': item['track_name'],
                'artist_name': item['artist_name'],
                'album_name': item['album_name'],
                'album_art_url': item['album_art_url']
            }).execute()

    return jsonify({'success': True, 'new_list_id': new_list_id})


@app.route('/api/list/<list_id>/reorder', methods=['POST'])
@login_required
def reorder_list(list_id):
    """Reorder a single item in a list."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    data = request.json
    item_id = data.get('item_id')
    new_position = data.get('new_position')

    # Update position
    supabase.table('list_items').update({'position': new_position}).eq('id', item_id).execute()

    return jsonify({'success': True})


@app.route('/api/list/<list_id>/reorder-all', methods=['POST'])
@login_required
def reorder_list_all(list_id):
    """Reorder all items in a list (for drag-and-drop)."""
    # Verify ownership
    list_result = supabase.table('lists').select('id').eq('id', list_id).eq('user_id', session['user']['id']).single().execute()
    if not list_result.data:
        return jsonify({'error': 'Access denied'}), 403

    data = request.json
    order = data.get('order', [])

    # Update each item's position
    for item in order:
        supabase.table('list_items').update({'position': item['position']}).eq('id', item['item_id']).execute()

    return jsonify({'success': True})


@app.route('/users')
def search_users():
    """Search for users by username."""
    query = request.args.get('q', '').strip()
    users = []

    if query:
        # Search for users with username containing the query
        result = supabase.table('profiles').select('*').ilike('username', f'%{query}%').limit(20).execute()
        users = result.data if result.data else []

        # Get public list count for each user
        for user in users:
            count_result = supabase.table('lists').select('id', count='exact').eq('user_id', user['id']).eq('is_public', True).execute()
            user['list_count'] = count_result.count if count_result.count else 0

    return render_template('search_users.html', users=users, query=query)


@app.route('/u/<username>')
def user_profile(username):
    """View a user's public profile and lists."""
    # Get user
    profile_result = supabase.table('profiles').select('*').eq('username', username).single().execute()

    if not profile_result.data:
        flash('User not found', 'error')
        return redirect(url_for('index'))

    profile = profile_result.data

    # Check if current user is viewing their own profile
    is_owner = 'user' in session and session['user']['id'] == profile['id']

    # Get their public lists
    lists_result = supabase.table('lists').select('*').eq('user_id', profile['id']).eq('is_public', True).order('created_at', desc=True).execute()
    lists = lists_result.data if lists_result.data else []

    # Get item counts, preview images, and like counts for each list
    for lst in lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(4).execute()
        lst['preview_images'] = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0
        # Get like count
        try:
            like_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', lst['id']).execute()
            lst['like_count'] = like_result.count if like_result.count else 0
        except Exception:
            lst['like_count'] = 0

    # Get favorite songs and albums (with error handling if table doesn't exist)
    favorite_songs = []
    favorite_albums = []
    try:
        fav_songs_result = supabase.table('profile_favorites').select('*').eq('user_id', profile['id']).eq('favorite_type', 'song').order('position').limit(5).execute()
        favorite_songs = fav_songs_result.data if fav_songs_result.data else []

        fav_albums_result = supabase.table('profile_favorites').select('*').eq('user_id', profile['id']).eq('favorite_type', 'album').order('position').limit(5).execute()
        favorite_albums = fav_albums_result.data if fav_albums_result.data else []
    except Exception:
        pass  # Table might not exist yet

    # Get album and song ratings
    album_ratings = []
    song_ratings = []
    try:
        album_ratings_result = supabase.table('album_ratings').select('*').eq('user_id', profile['id']).order('created_at', desc=True).execute()
        album_ratings = album_ratings_result.data if album_ratings_result.data else []

        song_ratings_result = supabase.table('song_ratings').select('*').eq('user_id', profile['id']).order('created_at', desc=True).execute()
        song_ratings = song_ratings_result.data if song_ratings_result.data else []
    except Exception:
        pass  # Tables might not exist yet

    # Get follower/following counts
    follower_count = 0
    following_count = 0
    is_following = False
    try:
        follower_result = supabase.table('followers').select('id', count='exact').eq('following_id', profile['id']).execute()
        follower_count = follower_result.count if follower_result.count else 0

        following_result = supabase.table('followers').select('id', count='exact').eq('follower_id', profile['id']).execute()
        following_count = following_result.count if following_result.count else 0

        # Check if current user is following this profile
        if 'user' in session and session['user']['id'] != profile['id']:
            follow_check = supabase.table('followers').select('id').eq('follower_id', session['user']['id']).eq('following_id', profile['id']).execute()
            is_following = bool(follow_check.data)
    except Exception:
        pass  # Table might not exist yet

    # Check if profile has Spotify linked
    has_spotify = bool(profile.get('spotify_user_id'))
    spotify_user_id = profile.get('spotify_user_id') if has_spotify else None

    # Check if current user has Spotify connected (for import/export features)
    current_user_has_spotify = False
    if 'user' in session:
        try:
            current_profile = supabase.table('profiles').select('spotify_user_id').eq('id', session['user']['id']).single().execute()
            current_user_has_spotify = bool(current_profile.data and current_profile.data.get('spotify_user_id'))
        except Exception:
            pass

    return render_template('profile.html', profile=profile, lists=lists,
                          favorite_songs=favorite_songs, favorite_albums=favorite_albums,
                          album_ratings=album_ratings, song_ratings=song_ratings, is_owner=is_owner,
                          follower_count=follower_count, following_count=following_count, is_following=is_following,
                          has_spotify=has_spotify, spotify_user_id=spotify_user_id,
                          current_user_has_spotify=current_user_has_spotify)


@app.route('/api/spotify/search/albums')
@login_required
def spotify_search_albums():
    """Search Spotify for albums."""
    query = request.args.get('q', '')
    if not query or len(query) < 2:
        return jsonify({'albums': []})

    try:
        results = spotify.search(q=query, type='album', limit=10)
        albums = []
        for item in results['albums']['items']:
            albums.append({
                'id': item['id'],
                'name': item['name'],
                'artist': ', '.join(a['name'] for a in item['artists']),
                'album_art': item['images'][0]['url'] if item['images'] else None,
                'year': item['release_date'][:4] if item.get('release_date') else ''
            })
        return jsonify({'albums': albums})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/profile/favorites', methods=['GET'])
@login_required
def get_favorites():
    """Get current user's favorites."""
    user_id = session['user']['id']

    try:
        songs = supabase.table('profile_favorites').select('*').eq('user_id', user_id).eq('favorite_type', 'song').order('position').execute()
        albums = supabase.table('profile_favorites').select('*').eq('user_id', user_id).eq('favorite_type', 'album').order('position').execute()

        return jsonify({
            'songs': songs.data if songs.data else [],
            'albums': albums.data if albums.data else []
        })
    except Exception:
        return jsonify({'songs': [], 'albums': [], 'error': 'Favorites table not set up yet'})


@app.route('/api/profile/favorites/<favorite_type>', methods=['POST'])
@login_required
def save_favorites(favorite_type):
    """Save favorites (songs or albums) for current user."""
    if favorite_type not in ['song', 'album']:
        return jsonify({'error': 'Invalid type'}), 400

    user_id = session['user']['id']
    data = request.json
    items = data.get('items', [])

    try:
        # Delete existing favorites of this type
        supabase.table('profile_favorites').delete().eq('user_id', user_id).eq('favorite_type', favorite_type).execute()

        # Insert new favorites
        for i, item in enumerate(items[:5]):  # Max 5
            supabase.table('profile_favorites').insert({
                'user_id': user_id,
                'favorite_type': favorite_type,
                'position': i + 1,
                'spotify_id': item.get('spotify_id'),
                'name': item.get('name'),
                'artist_name': item.get('artist_name'),
                'album_art_url': item.get('album_art_url')
            }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': 'Favorites table not set up. Please create the profile_favorites table in Supabase.'}), 500


@app.route('/api/profile/favorites/<favorite_type>/<int:position>', methods=['DELETE'])
@login_required
def remove_favorite(favorite_type, position):
    """Remove a favorite by position."""
    if favorite_type not in ['song', 'album']:
        return jsonify({'error': 'Invalid type'}), 400

    user_id = session['user']['id']
    supabase.table('profile_favorites').delete().eq('user_id', user_id).eq('favorite_type', favorite_type).eq('position', position).execute()

    return jsonify({'success': True})


# ============== USER LISTS API ==============

@app.route('/api/user/lists')
@login_required
def get_user_lists():
    """Get all lists for the current user (for add-to-list modal)."""
    user_id = session['user']['id']

    result = supabase.table('lists').select('id, title').eq('user_id', user_id).order('created_at', desc=True).execute()
    lists = result.data if result.data else []

    # Get item counts and first image for each list
    for lst in lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(1).execute()
        lst['preview_image'] = items_result.data[0]['album_art_url'] if items_result.data else None
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0

    return jsonify({'lists': lists})


@app.route('/api/list/create-with-track', methods=['POST'])
@login_required
def create_list_with_track():
    """Create a new list and add a track to it."""
    data = request.json
    title = data.get('title')
    track = data.get('track')

    if not title or not track:
        return jsonify({'error': 'Title and track required'}), 400

    try:
        # Create the list
        list_result = supabase.table('lists').insert({
            'user_id': session['user']['id'],
            'title': title,
            'is_ranked': True,
            'is_public': False
        }).execute()

        if not list_result.data:
            return jsonify({'error': 'Failed to create list'}), 500

        list_id = list_result.data[0]['id']

        # Add the track
        supabase.table('list_items').insert({
            'list_id': list_id,
            'spotify_track_id': track.get('trackId'),
            'track_name': track.get('trackName'),
            'artist_name': track.get('artistName'),
            'album_name': track.get('albumName'),
            'album_art_url': track.get('albumArt'),
            'position': 1
        }).execute()

        return jsonify({'success': True, 'list_id': list_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============== LISTEN LIST API ==============

@app.route('/api/listen-list/add', methods=['POST'])
@login_required
def add_to_listen_list():
    """Add an album to user's listen-list (albums to listen to later)."""
    user_id = session['user']['id']
    data = request.json

    try:
        # Check if already in listen list
        existing = supabase.table('listen_list').select('id').eq('user_id', user_id).eq('album_name', data.get('album_name')).eq('artist_name', data.get('artist_name')).execute()

        if existing.data:
            return jsonify({'success': True, 'message': 'Already in listen-list'})

        # Add to listen list
        supabase.table('listen_list').insert({
            'user_id': user_id,
            'album_name': data.get('album_name'),
            'artist_name': data.get('artist_name'),
            'album_art_url': data.get('album_art_url')
        }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/listen-list', methods=['GET'])
@login_required
def get_listen_list():
    """Get user's listen-list."""
    user_id = session['user']['id']

    try:
        result = supabase.table('listen_list').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
        return jsonify({'items': result.data if result.data else []})
    except Exception:
        return jsonify({'items': []})


@app.route('/api/listen-list/<item_id>', methods=['DELETE'])
@login_required
def remove_from_listen_list(item_id):
    """Remove an album from listen-list."""
    user_id = session['user']['id']
    supabase.table('listen_list').delete().eq('id', item_id).eq('user_id', user_id).execute()
    return jsonify({'success': True})


# ============== ALBUM RATINGS API ==============

@app.route('/api/album/rating', methods=['GET'])
@login_required
def get_album_rating():
    """Get user's rating for an album."""
    user_id = session['user']['id']
    album_name = request.args.get('album')
    artist_name = request.args.get('artist')

    try:
        result = supabase.table('album_ratings').select('rating').eq('user_id', user_id).eq('album_name', album_name).eq('artist_name', artist_name).single().execute()

        if result.data:
            return jsonify({'rating': result.data['rating']})
        return jsonify({'rating': None})
    except Exception:
        return jsonify({'rating': None})


@app.route('/api/album/rating', methods=['POST'])
@login_required
def save_album_rating():
    """Save or update user's rating for an album."""
    user_id = session['user']['id']
    data = request.json

    album_name = data.get('album_name')
    artist_name = data.get('artist_name')
    rating = data.get('rating')

    try:
        # Check if rating exists
        existing = supabase.table('album_ratings').select('id').eq('user_id', user_id).eq('album_name', album_name).eq('artist_name', artist_name).execute()

        if rating == 0:
            # Delete rating if set to 0
            if existing.data:
                supabase.table('album_ratings').delete().eq('id', existing.data[0]['id']).execute()
            return jsonify({'success': True})

        if existing.data:
            # Update existing rating
            supabase.table('album_ratings').update({
                'rating': rating,
                'album_art_url': data.get('album_art_url')
            }).eq('id', existing.data[0]['id']).execute()
        else:
            # Insert new rating
            supabase.table('album_ratings').insert({
                'user_id': user_id,
                'album_name': album_name,
                'artist_name': artist_name,
                'album_art_url': data.get('album_art_url'),
                'rating': rating
            }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/album/ratings')
@login_required
def get_user_ratings():
    """Get all of user's album ratings."""
    user_id = session['user']['id']

    try:
        result = supabase.table('album_ratings').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
        return jsonify({'ratings': result.data if result.data else []})
    except Exception:
        return jsonify({'ratings': []})


# ============== UNIFIED SEARCH API ==============

@app.route('/api/search/unified')
def unified_search():
    """Unified search across profiles, lists, songs, and albums."""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'profiles': [], 'lists': [], 'songs': [], 'albums': []})

    results = {'profiles': [], 'lists': [], 'songs': [], 'albums': []}

    try:
        # Search profiles
        profiles_result = supabase.table('profiles').select('*').ilike('username', f'%{query}%').limit(5).execute()
        if profiles_result.data:
            for p in profiles_result.data:
                count_result = supabase.table('lists').select('id', count='exact').eq('user_id', p['id']).eq('is_public', True).execute()
                results['profiles'].append({
                    'username': p['username'],
                    'list_count': count_result.count if count_result.count else 0
                })

        # Search lists (public only)
        lists_result = supabase.table('lists').select('*, profiles(username)').ilike('title', f'%{query}%').eq('is_public', True).limit(5).execute()
        if lists_result.data:
            for l in lists_result.data:
                # Get preview image
                items_result = supabase.table('list_items').select('album_art_url').eq('list_id', l['id']).order('position').limit(1).execute()
                preview_image = items_result.data[0]['album_art_url'] if items_result.data else None
                # Get item count
                count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', l['id']).execute()
                # Get like count
                like_count = 0
                try:
                    like_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', l['id']).execute()
                    like_count = like_result.count if like_result.count else 0
                except Exception:
                    pass
                results['lists'].append({
                    'id': l['id'],
                    'title': l['title'],
                    'username': l['profiles']['username'] if l.get('profiles') else 'Unknown',
                    'preview_image': preview_image,
                    'item_count': count_result.count if count_result.count else 0,
                    'like_count': like_count
                })

        # Search songs via Spotify
        try:
            spotify_results = spotify.search(q=query, type='track', limit=5)
            for item in spotify_results['tracks']['items']:
                results['songs'].append({
                    'id': item['id'],
                    'name': item['name'],
                    'artist': ', '.join(a['name'] for a in item['artists']),
                    'album': item['album']['name'],
                    'album_art': item['album']['images'][0]['url'] if item['album']['images'] else None
                })
        except Exception:
            pass

        # Search albums via Spotify
        try:
            album_results = spotify.search(q=query, type='album', limit=5)
            for item in album_results['albums']['items']:
                results['albums'].append({
                    'id': item['id'],
                    'name': item['name'],
                    'artist': ', '.join(a['name'] for a in item['artists']),
                    'album_art': item['images'][0]['url'] if item['images'] else None
                })
        except Exception:
            pass

    except Exception as e:
        print(f"Search error: {e}")

    return jsonify(results)


@app.route('/api/item/details')
def item_details():
    """Get details for a song or album including average rating and lists containing it."""
    item_type = request.args.get('type')  # 'song' or 'album'
    name = request.args.get('name')
    artist = request.args.get('artist')

    if not item_type or not name or not artist:
        return jsonify({'error': 'Missing parameters'}), 400

    result = {
        'avg_rating': None,
        'rating_count': 0,
        'user_rating': None,
        'lists': []
    }

    try:
        # Get average rating
        if item_type == 'song':
            ratings_result = supabase.table('song_ratings').select('rating').eq('track_name', name).eq('artist_name', artist).execute()
        else:
            ratings_result = supabase.table('album_ratings').select('rating').eq('album_name', name).eq('artist_name', artist).execute()

        if ratings_result.data:
            ratings = [r['rating'] for r in ratings_result.data]
            result['avg_rating'] = sum(ratings) / len(ratings)
            result['rating_count'] = len(ratings)

        # Get user's rating if logged in
        if 'user' in session:
            user_id = session['user']['id']
            if item_type == 'song':
                user_rating = supabase.table('song_ratings').select('rating').eq('user_id', user_id).eq('track_name', name).eq('artist_name', artist).execute()
            else:
                user_rating = supabase.table('album_ratings').select('rating').eq('user_id', user_id).eq('album_name', name).eq('artist_name', artist).execute()

            if user_rating.data:
                result['user_rating'] = user_rating.data[0]['rating']

        # Get lists containing this item, sorted by like count
        if item_type == 'song':
            list_items = supabase.table('list_items').select('list_id').eq('track_name', name).eq('artist_name', artist).execute()
        else:
            list_items = supabase.table('list_items').select('list_id').eq('album_name', name).eq('artist_name', artist).execute()

        if list_items.data:
            list_ids = list(set([item['list_id'] for item in list_items.data]))
            lists_with_likes = []

            for list_id in list_ids[:20]:  # Limit to 20 lists
                list_data = supabase.table('lists').select('*, profiles(username)').eq('id', list_id).eq('is_public', True).single().execute()
                if list_data.data:
                    # Get like count
                    like_count = 0
                    try:
                        like_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', list_id).execute()
                        like_count = like_result.count if like_result.count else 0
                    except Exception:
                        pass

                    # Get preview image and item count
                    items_result = supabase.table('list_items').select('album_art_url').eq('list_id', list_id).order('position').limit(1).execute()
                    preview_image = items_result.data[0]['album_art_url'] if items_result.data else None
                    count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', list_id).execute()

                    lists_with_likes.append({
                        'id': list_id,
                        'title': list_data.data['title'],
                        'username': list_data.data['profiles']['username'] if list_data.data.get('profiles') else 'Unknown',
                        'preview_image': preview_image,
                        'item_count': count_result.count if count_result.count else 0,
                        'like_count': like_count
                    })

            # Sort by like count descending
            result['lists'] = sorted(lists_with_likes, key=lambda x: x['like_count'], reverse=True)

    except Exception as e:
        print(f"Item details error: {e}")

    return jsonify(result)


# ============== LIST LIKES API ==============

@app.route('/api/list/<list_id>/like', methods=['POST'])
@login_required
def like_list(list_id):
    """Like a list."""
    user_id = session['user']['id']

    try:
        # Check if already liked
        existing = supabase.table('list_likes').select('id').eq('user_id', user_id).eq('list_id', list_id).execute()

        if existing.data:
            return jsonify({'success': True, 'message': 'Already liked'})

        # Add like
        supabase.table('list_likes').insert({
            'user_id': user_id,
            'list_id': list_id
        }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/list/<list_id>/unlike', methods=['POST'])
@login_required
def unlike_list(list_id):
    """Unlike a list."""
    user_id = session['user']['id']

    try:
        supabase.table('list_likes').delete().eq('user_id', user_id).eq('list_id', list_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/list/<list_id>/like-status')
def get_like_status(list_id):
    """Get like status and count for a list."""
    try:
        # Get like count
        count_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', list_id).execute()
        like_count = count_result.count if count_result.count else 0

        # Check if current user has liked
        user_liked = False
        if 'user' in session:
            user_id = session['user']['id']
            existing = supabase.table('list_likes').select('id').eq('user_id', user_id).eq('list_id', list_id).execute()
            user_liked = bool(existing.data)

        return jsonify({
            'like_count': like_count,
            'user_liked': user_liked
        })
    except Exception:
        return jsonify({'like_count': 0, 'user_liked': False})


@app.route('/api/user/<user_id>/liked-lists')
def get_user_liked_lists(user_id):
    """Get lists that a user has liked."""
    try:
        # Get liked list IDs
        likes_result = supabase.table('list_likes').select('list_id').eq('user_id', user_id).order('created_at', desc=True).execute()

        if not likes_result.data:
            return jsonify({'lists': []})

        lists = []
        for like in likes_result.data:
            list_data = supabase.table('lists').select('*, profiles(username)').eq('id', like['list_id']).eq('is_public', True).single().execute()
            if list_data.data:
                # Get preview and count
                items_result = supabase.table('list_items').select('album_art_url').eq('list_id', like['list_id']).order('position').limit(4).execute()
                preview_images = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]
                count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', like['list_id']).execute()

                # Get like count
                like_count_result = supabase.table('list_likes').select('id', count='exact').eq('list_id', like['list_id']).execute()

                lists.append({
                    'id': like['list_id'],
                    'title': list_data.data['title'],
                    'description': list_data.data.get('description'),
                    'is_ranked': list_data.data['is_ranked'],
                    'username': list_data.data['profiles']['username'] if list_data.data.get('profiles') else 'Unknown',
                    'preview_images': preview_images,
                    'item_count': count_result.count if count_result.count else 0,
                    'like_count': like_count_result.count if like_count_result.count else 0
                })

        return jsonify({'lists': lists})
    except Exception as e:
        return jsonify({'lists': [], 'error': str(e)})


# ============== SONG RATINGS API ==============

@app.route('/api/song/rating', methods=['GET'])
@login_required
def get_song_rating():
    """Get user's rating for a song."""
    user_id = session['user']['id']
    track_name = request.args.get('track')
    artist_name = request.args.get('artist')

    try:
        result = supabase.table('song_ratings').select('rating').eq('user_id', user_id).eq('track_name', track_name).eq('artist_name', artist_name).single().execute()

        if result.data:
            return jsonify({'rating': result.data['rating']})
        return jsonify({'rating': None})
    except Exception:
        return jsonify({'rating': None})


@app.route('/api/song/rating', methods=['POST'])
@login_required
def save_song_rating():
    """Save or update user's rating for a song."""
    user_id = session['user']['id']
    data = request.json

    track_name = data.get('track_name')
    artist_name = data.get('artist_name')
    rating = data.get('rating')

    try:
        # Check if rating exists
        existing = supabase.table('song_ratings').select('id').eq('user_id', user_id).eq('track_name', track_name).eq('artist_name', artist_name).execute()

        if rating == 0:
            # Delete rating if set to 0
            if existing.data:
                supabase.table('song_ratings').delete().eq('id', existing.data[0]['id']).execute()
            return jsonify({'success': True})

        if existing.data:
            # Update existing rating
            supabase.table('song_ratings').update({
                'rating': rating,
                'album_art_url': data.get('album_art_url')
            }).eq('id', existing.data[0]['id']).execute()
        else:
            # Insert new rating
            supabase.table('song_ratings').insert({
                'user_id': user_id,
                'track_name': track_name,
                'artist_name': artist_name,
                'album_art_url': data.get('album_art_url'),
                'rating': rating
            }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/song/ratings')
@login_required
def get_user_song_ratings():
    """Get all of user's song ratings."""
    user_id = session['user']['id']

    try:
        result = supabase.table('song_ratings').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
        return jsonify({'ratings': result.data if result.data else []})
    except Exception:
        return jsonify({'ratings': []})


# ============== FOLLOW API ==============

@app.route('/api/user/<user_id>/follow', methods=['POST'])
@login_required
def follow_user(user_id):
    """Follow a user."""
    follower_id = session['user']['id']

    # Can't follow yourself
    if follower_id == user_id:
        return jsonify({'error': 'Cannot follow yourself'}), 400

    try:
        # Check if already following
        existing = supabase.table('followers').select('id').eq('follower_id', follower_id).eq('following_id', user_id).execute()

        if existing.data:
            return jsonify({'success': True, 'message': 'Already following'})

        # Add follow
        supabase.table('followers').insert({
            'follower_id': follower_id,
            'following_id': user_id
        }).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/<user_id>/unfollow', methods=['POST'])
@login_required
def unfollow_user(user_id):
    """Unfollow a user."""
    follower_id = session['user']['id']

    try:
        supabase.table('followers').delete().eq('follower_id', follower_id).eq('following_id', user_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/<user_id>/followers')
def get_user_followers(user_id):
    """Get a user's followers."""
    try:
        result = supabase.table('followers').select('follower_id').eq('following_id', user_id).execute()

        if not result.data:
            return jsonify({'followers': []})

        followers = []
        for f in result.data:
            profile = supabase.table('profiles').select('username').eq('id', f['follower_id']).single().execute()
            if profile.data:
                followers.append({
                    'id': f['follower_id'],
                    'username': profile.data['username']
                })

        return jsonify({'followers': followers})
    except Exception as e:
        return jsonify({'followers': [], 'error': str(e)})


@app.route('/api/user/<user_id>/following')
def get_user_following(user_id):
    """Get users that a user is following."""
    try:
        result = supabase.table('followers').select('following_id').eq('follower_id', user_id).execute()

        if not result.data:
            return jsonify({'following': []})

        following = []
        for f in result.data:
            profile = supabase.table('profiles').select('username').eq('id', f['following_id']).single().execute()
            if profile.data:
                following.append({
                    'id': f['following_id'],
                    'username': profile.data['username']
                })

        return jsonify({'following': following})
    except Exception as e:
        return jsonify({'following': [], 'error': str(e)})


# ============== SPOTIFY OAUTH ROUTES ==============

@app.route('/connect/spotify')
@login_required
def connect_spotify():
    """Redirect to Spotify authorization."""
    return redirect(get_spotify_auth_url())


@app.route('/callback/spotify')
def spotify_callback():
    """Handle Spotify OAuth callback."""
    error = request.args.get('error')
    if error:
        flash(f'Spotify authorization failed: {error}', 'error')
        return redirect(url_for('dashboard'))

    code = request.args.get('code')
    if not code:
        flash('No authorization code received', 'error')
        return redirect(url_for('dashboard'))

    if 'user' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))

    # Exchange code for tokens
    token_data = exchange_code_for_tokens(code)
    if not token_data:
        flash('Failed to get Spotify tokens', 'error')
        return redirect(url_for('dashboard'))

    # Get Spotify user info
    sp = spotipy.Spotify(auth=token_data['access_token'])
    try:
        spotify_user = sp.current_user()
    except Exception as e:
        flash(f'Failed to get Spotify profile: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

    # Calculate token expiration
    expires_at = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])

    # Save to database
    try:
        supabase.table('profiles').update({
            'spotify_user_id': spotify_user['id'],
            'spotify_display_name': spotify_user.get('display_name', spotify_user['id']),
            'spotify_access_token': token_data['access_token'],
            'spotify_refresh_token': token_data['refresh_token'],
            'spotify_token_expires': expires_at.isoformat()
        }).eq('id', session['user']['id']).execute()

        flash('Spotify account linked successfully!', 'success')
    except Exception as e:
        flash(f'Failed to save Spotify connection: {str(e)}', 'error')

    return redirect(url_for('user_profile', username=session['user']['username']))


@app.route('/disconnect/spotify', methods=['POST'])
@login_required
def disconnect_spotify():
    """Disconnect Spotify account."""
    try:
        supabase.table('profiles').update({
            'spotify_user_id': None,
            'spotify_display_name': None,
            'spotify_access_token': None,
            'spotify_refresh_token': None,
            'spotify_token_expires': None
        }).eq('id', session['user']['id']).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/spotify/connected')
@login_required
def check_spotify_connected():
    """Check if current user has Spotify connected."""
    try:
        profile = supabase.table('profiles').select('spotify_user_id').eq('id', session['user']['id']).single().execute()
        return jsonify({'connected': bool(profile.data and profile.data.get('spotify_user_id'))})
    except Exception:
        return jsonify({'connected': False})


# ============== SPOTIFY IMPORT/EXPORT ROUTES ==============

@app.route('/api/spotify/playlists')
@login_required
def get_spotify_playlists():
    """Get user's Spotify playlists for import."""
    sp = get_user_spotify_client(session['user']['id'])
    if not sp:
        return jsonify({'error': 'Spotify not connected'}), 401

    try:
        playlists = []
        results = sp.current_user_playlists(limit=50)

        while results:
            for playlist in results['items']:
                if playlist:
                    playlists.append({
                        'id': playlist['id'],
                        'name': playlist['name'],
                        'track_count': playlist['tracks']['total'],
                        'image': playlist['images'][0]['url'] if playlist.get('images') else None,
                        'owner': playlist['owner']['display_name']
                    })

            if results['next']:
                results = sp.next(results)
            else:
                break

        return jsonify({'playlists': playlists})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/spotify/playlist/<playlist_id>/tracks')
@login_required
def get_spotify_playlist_tracks(playlist_id):
    """Get tracks from a Spotify playlist."""
    sp = get_user_spotify_client(session['user']['id'])
    if not sp:
        return jsonify({'error': 'Spotify not connected'}), 401

    try:
        tracks = []
        results = sp.playlist_tracks(playlist_id, limit=100)

        while results:
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id'):
                    tracks.append({
                        'id': track['id'],
                        'name': track['name'],
                        'artist': ', '.join(a['name'] for a in track['artists']),
                        'album': track['album']['name'],
                        'album_art': track['album']['images'][0]['url'] if track['album'].get('images') else None
                    })

            if results['next']:
                results = sp.next(results)
            else:
                break

        return jsonify({'tracks': tracks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/spotify/import', methods=['POST'])
@login_required
def import_spotify_playlist():
    """Import a Spotify playlist to Jukebox."""
    sp = get_user_spotify_client(session['user']['id'])
    if not sp:
        return jsonify({'error': 'Spotify not connected'}), 401

    data = request.json
    playlist_id = data.get('playlist_id')
    target_list_id = data.get('list_id')  # None means create new
    new_list_title = data.get('new_list_title')

    if not playlist_id:
        return jsonify({'error': 'Playlist ID required'}), 400

    try:
        # Get playlist info and tracks
        playlist_info = sp.playlist(playlist_id)
        tracks = []
        results = sp.playlist_tracks(playlist_id, limit=100)

        while results:
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id'):
                    tracks.append({
                        'spotify_track_id': track['id'],
                        'track_name': track['name'],
                        'artist_name': ', '.join(a['name'] for a in track['artists']),
                        'album_name': track['album']['name'],
                        'album_art_url': track['album']['images'][0]['url'] if track['album'].get('images') else None
                    })

            if results['next']:
                results = sp.next(results)
            else:
                break

        if target_list_id:
            # Add to existing list
            list_result = supabase.table('lists').select('id').eq('id', target_list_id).eq('user_id', session['user']['id']).single().execute()
            if not list_result.data:
                return jsonify({'error': 'List not found or access denied'}), 403

            # Get current max position
            pos_result = supabase.table('list_items').select('position').eq('list_id', target_list_id).order('position', desc=True).limit(1).execute()
            next_position = (pos_result.data[0]['position'] + 1) if pos_result.data else 1

            # Add tracks
            for i, track in enumerate(tracks):
                supabase.table('list_items').insert({
                    'list_id': target_list_id,
                    'position': next_position + i,
                    **track
                }).execute()

            return jsonify({'success': True, 'list_id': target_list_id, 'tracks_added': len(tracks)})
        else:
            # Create new list
            title = new_list_title or playlist_info['name']
            list_result = supabase.table('lists').insert({
                'user_id': session['user']['id'],
                'title': title,
                'description': f"Imported from Spotify: {playlist_info['name']}",
                'is_ranked': True,
                'is_public': False
            }).execute()

            if not list_result.data:
                return jsonify({'error': 'Failed to create list'}), 500

            new_list_id = list_result.data[0]['id']

            # Add tracks
            for i, track in enumerate(tracks):
                supabase.table('list_items').insert({
                    'list_id': new_list_id,
                    'position': i + 1,
                    **track
                }).execute()

            return jsonify({'success': True, 'list_id': new_list_id, 'tracks_added': len(tracks)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/spotify/export', methods=['POST'])
@login_required
def export_to_spotify():
    """Export a Jukebox list to Spotify."""
    sp = get_user_spotify_client(session['user']['id'])
    if not sp:
        return jsonify({'error': 'Spotify not connected'}), 401

    data = request.json
    list_id = data.get('list_id')
    target_playlist_id = data.get('playlist_id')  # None means create new
    new_playlist_name = data.get('new_playlist_name')

    if not list_id:
        return jsonify({'error': 'List ID required'}), 400

    try:
        # Get list and items
        list_result = supabase.table('lists').select('*').eq('id', list_id).single().execute()
        if not list_result.data:
            return jsonify({'error': 'List not found'}), 404

        lst = list_result.data

        # Check access - must be owner or list must be public
        is_owner = session['user']['id'] == lst['user_id']
        if not lst['is_public'] and not is_owner:
            return jsonify({'error': 'Access denied'}), 403

        items_result = supabase.table('list_items').select('spotify_track_id').eq('list_id', list_id).order('position').execute()
        track_ids = [item['spotify_track_id'] for item in (items_result.data or []) if item.get('spotify_track_id')]

        if not track_ids:
            return jsonify({'error': 'No tracks to export'}), 400

        # Get current user's Spotify ID
        spotify_user = sp.current_user()
        spotify_user_id = spotify_user['id']

        if target_playlist_id:
            # Update existing playlist - replace all tracks
            sp.playlist_replace_items(target_playlist_id, [])

            # Add tracks in batches of 100
            for i in range(0, len(track_ids), 100):
                batch = [f'spotify:track:{tid}' for tid in track_ids[i:i+100]]
                sp.playlist_add_items(target_playlist_id, batch)

            return jsonify({'success': True, 'playlist_id': target_playlist_id, 'tracks_exported': len(track_ids)})
        else:
            # Create new playlist
            name = new_playlist_name or lst['title']
            playlist = sp.user_playlist_create(
                spotify_user_id,
                name,
                public=False,
                description=f"Exported from Jukebox: {lst['title']}"
            )

            # Add tracks in batches of 100
            for i in range(0, len(track_ids), 100):
                batch = [f'spotify:track:{tid}' for tid in track_ids[i:i+100]]
                sp.playlist_add_items(playlist['id'], batch)

            return jsonify({
                'success': True,
                'playlist_id': playlist['id'],
                'playlist_url': playlist['external_urls']['spotify'],
                'tracks_exported': len(track_ids)
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/spotify/my-playlists')
@login_required
def get_my_spotify_playlists():
    """Get only playlists owned by the current user (for export target selection)."""
    sp = get_user_spotify_client(session['user']['id'])
    if not sp:
        return jsonify({'error': 'Spotify not connected'}), 401

    try:
        spotify_user = sp.current_user()
        spotify_user_id = spotify_user['id']

        playlists = []
        results = sp.current_user_playlists(limit=50)

        while results:
            for playlist in results['items']:
                if playlist and playlist['owner']['id'] == spotify_user_id:
                    playlists.append({
                        'id': playlist['id'],
                        'name': playlist['name'],
                        'track_count': playlist['tracks']['total'],
                        'image': playlist['images'][0]['url'] if playlist.get('images') else None
                    })

            if results['next']:
                results = sp.next(results)
            else:
                break

        return jsonify({'playlists': playlists})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    import os
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, port=5000)
