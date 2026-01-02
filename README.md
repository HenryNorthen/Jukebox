# Jukebox - Music List App

A simple Flask app for you and your friends to create and share ranked music lists.

## Setup (15-20 minutes)

### 1. Install Python dependencies

```bash
cd app
pip install -r requirements.txt
```

### 2. Set up Supabase database

1. Go to your Supabase project: https://supabase.com/dashboard
2. Click **SQL Editor** in the sidebar
3. Copy the entire contents of `schema.sql` and paste it into the editor
4. Click **Run** to create the tables

### 3. Configure environment variables

1. Copy `.env.example` to `.env`:
   ```bash
   copy .env.example .env
   ```

2. Edit `.env` with your credentials:
   ```
   SUPABASE_URL=https://kmufppsccrthudshqlah.supabase.co
   SUPABASE_KEY=your-anon-key-here

   SPOTIFY_CLIENT_ID=52ab20d869af488b9ca318b879e11b55
   SPOTIFY_CLIENT_SECRET=your-secret-here

   FLASK_SECRET_KEY=any-random-string-here
   ```

   **Where to find these:**
   - Supabase: Project Settings > API > `anon` `public` key
   - Spotify: https://developer.spotify.com/dashboard > Your app > Settings

### 4. Run the app

```bash
python app.py
```

Visit http://localhost:5000

## Features

- User signup/login (email + password)
- Create ranked or unranked lists
- Search Spotify to add songs
- Beautiful album art grid display
- Public/private lists
- Shareable profile pages (e.g., `/u/henry`)

## File Structure

```
app/
├── app.py              # Main Flask application
├── config.py           # Configuration loader
├── schema.sql          # Database schema (run in Supabase)
├── requirements.txt    # Python dependencies
├── .env.example        # Template for environment variables
├── .env                # Your actual credentials (don't commit!)
└── templates/          # HTML templates
    ├── base.html       # Base layout
    ├── index.html      # Landing page
    ├── login.html      # Login form
    ├── signup.html     # Registration form
    ├── dashboard.html  # User's lists
    ├── create_list.html
    ├── view_list.html  # Public list view
    ├── edit_list.html  # Add/remove songs
    └── profile.html    # User profile
```

## Sharing with friends

For now, friends can access your local server if you're on the same network.

To deploy publicly (free):
1. Create account at https://render.com
2. Connect your GitHub repo
3. Add environment variables in Render dashboard
4. Deploy!

Or use https://railway.app or https://fly.io (also free tiers available).
