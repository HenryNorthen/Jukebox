from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
from supabase import create_client, Client
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from datetime import timedelta
from config import Config

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

    # Get item counts and preview images for each list
    for lst in public_lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(4).execute()
        lst['preview_images'] = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0

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

    # Get item counts and preview images for each list
    for lst in lists:
        items_result = supabase.table('list_items').select('album_art_url').eq('list_id', lst['id']).order('position').limit(4).execute()
        lst['preview_images'] = [item['album_art_url'] for item in (items_result.data or []) if item.get('album_art_url')]
        count_result = supabase.table('list_items').select('id', count='exact').eq('list_id', lst['id']).execute()
        lst['item_count'] = count_result.count if count_result.count else 0

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

    return render_template('profile.html', profile=profile, lists=lists,
                          favorite_songs=favorite_songs, favorite_albums=favorite_albums, is_owner=is_owner)


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


if __name__ == '__main__':
    import os
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, port=5000)
