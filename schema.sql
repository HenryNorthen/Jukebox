-- Run this in your Supabase SQL Editor (https://supabase.com/dashboard/project/YOUR_PROJECT/sql)

-- Profiles table (extends Supabase auth.users)
CREATE TABLE profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    username TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    bio TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Lists table
CREATE TABLE lists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    is_ranked BOOLEAN DEFAULT true,
    is_public BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- List items (songs in a list)
CREATE TABLE list_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    list_id UUID NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    spotify_track_id TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    album_name TEXT,
    album_art_url TEXT,
    note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_lists_user_id ON lists(user_id);
CREATE INDEX idx_lists_public ON lists(is_public) WHERE is_public = true;
CREATE INDEX idx_list_items_list_id ON list_items(list_id);
CREATE INDEX idx_list_items_position ON list_items(list_id, position);

-- Row Level Security (RLS) policies
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE lists ENABLE ROW LEVEL SECURITY;
ALTER TABLE list_items ENABLE ROW LEVEL SECURITY;

-- Profiles: anyone can read, only owner can update
CREATE POLICY "Public profiles are viewable by everyone" ON profiles
    FOR SELECT USING (true);

CREATE POLICY "Users can update own profile" ON profiles
    FOR UPDATE USING (auth.uid() = id);

CREATE POLICY "Users can insert own profile" ON profiles
    FOR INSERT WITH CHECK (auth.uid() = id);

-- Lists: public lists viewable by all, private lists only by owner
CREATE POLICY "Public lists are viewable by everyone" ON lists
    FOR SELECT USING (is_public = true OR auth.uid() = user_id);

CREATE POLICY "Users can create own lists" ON lists
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own lists" ON lists
    FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own lists" ON lists
    FOR DELETE USING (auth.uid() = user_id);

-- List items: viewable if list is viewable, editable by list owner
CREATE POLICY "List items viewable with list" ON list_items
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM lists
            WHERE lists.id = list_items.list_id
            AND (lists.is_public = true OR lists.user_id = auth.uid())
        )
    );

CREATE POLICY "Users can add items to own lists" ON list_items
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM lists
            WHERE lists.id = list_items.list_id
            AND lists.user_id = auth.uid()
        )
    );

CREATE POLICY "Users can update items in own lists" ON list_items
    FOR UPDATE USING (
        EXISTS (
            SELECT 1 FROM lists
            WHERE lists.id = list_items.list_id
            AND lists.user_id = auth.uid()
        )
    );

CREATE POLICY "Users can delete items from own lists" ON list_items
    FOR DELETE USING (
        EXISTS (
            SELECT 1 FROM lists
            WHERE lists.id = list_items.list_id
            AND lists.user_id = auth.uid()
        )
    );
