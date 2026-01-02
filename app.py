from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
from supabase import create_client, Client
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

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
    """Landing page - show public lists or redirect to dashboard."""
    if 'user' in session:
        return redirect(url_for('dashboard'))

    # Get some public lists to display
    result = supabase.table('lists').select('*, profiles(username)').eq('is_public', True).limit(10).execute()
    public_lists = result.data if result.data else []

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

        try:
            auth_response = supabase.auth.sign_in_with_password({
                'email': email,
                'password': password
            })

            if auth_response.user:
                # Get profile
                profile = supabase.table('profiles').select('*').eq('id', auth_response.user.id).single().execute()

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

        result = supabase.table('lists').insert({
            'user_id': session['user']['id'],
            'title': title,
            'description': description,
            'is_ranked': is_ranked,
            'is_public': is_public
        }).execute()

        if result.data:
            return redirect(url_for('edit_list', list_id=result.data[0]['id']))

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


@app.route('/u/<username>')
def user_profile(username):
    """View a user's public profile and lists."""
    # Get user
    profile_result = supabase.table('profiles').select('*').eq('username', username).single().execute()

    if not profile_result.data:
        flash('User not found', 'error')
        return redirect(url_for('index'))

    profile = profile_result.data

    # Get their public lists
    lists_result = supabase.table('lists').select('*').eq('user_id', profile['id']).eq('is_public', True).order('created_at', desc=True).execute()
    lists = lists_result.data if lists_result.data else []

    return render_template('profile.html', profile=profile, lists=lists)


if __name__ == '__main__':
    import os
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, port=5000)
