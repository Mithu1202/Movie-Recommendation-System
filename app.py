import pandas as pd
import streamlit as st
import pickle
import requests
import sqlite3
import bcrypt
import os
from dotenv import load_dotenv
import openai

# Load environment variables from .env file
load_dotenv()

# Retrieve the OpenAI API key from the environment variable
openai.api_key = os.getenv("OPENAI_API_KEY")

# Create a connection to the database
conn = sqlite3.connect('movie_recommendation.db', check_same_thread=False)
c = conn.cursor()

# Create tables if they don't exist
c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT
            )''')

c.execute('''CREATE TABLE IF NOT EXISTS watch_history (
                user_id INTEGER,
                movie_id INTEGER,
                PRIMARY KEY (user_id, movie_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')

c.execute('''CREATE TABLE IF NOT EXISTS ratings (
                user_id INTEGER,
                movie_id INTEGER,
                rating REAL,
                PRIMARY KEY (user_id, movie_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
            )''')
conn.commit()

# Function to fetch movie poster
def fetch_poster(movie_id):
    response = requests.get(f'https://api.themoviedb.org/3/movie/{movie_id}?api_key=020b311fe0559698373a16008dc6a672&language=en-US')
    data = response.json()
    return "https://image.tmdb.org/t/p/w500/" + data['poster_path']

# Movie recommendation based on similarity with a threshold
def recommend(movie, threshold=0.2):
    movie_index = movies[movies['title'] == movie].index[0]
    distances = similarity[movie_index]
    
    # Filter movies based on the threshold
    movies_list = [
        (i, dist) for i, dist in enumerate(distances) 
        if dist >= threshold and i != movie_index
    ]
    
    # Sort the movies by similarity score in descending order
    movies_list = sorted(movies_list, key=lambda x: x[1], reverse=True)[:5]

    recommended_movies = []
    recommended_movies_posters = []
    for x in movies_list:
        movie_id = movies.iloc[x[0]].movie_id
        recommended_movies.append(movies.iloc[x[0]].title)
        recommended_movies_posters.append(fetch_poster(movie_id))
    
    return recommended_movies, recommended_movies_posters


# Function to recommend movies based on user history
def recommend_based_on_history(user_id):
    watched_movie_ids = [row[0] for row in c.execute('SELECT movie_id FROM watch_history WHERE user_id=?', (user_id,))]
    if not watched_movie_ids:
        return [], []

    recommended_movies, recommended_movies_posters = [], []
    for movie_id in watched_movie_ids:
        if movie_id in movies['movie_id'].values:
            movie_title = movies[movies['movie_id'] == movie_id]['title'].values[0]
            names, posters = recommend(movie_title)
            recommended_movies.extend(names[:2])
            recommended_movies_posters.extend(posters[:2])

    return recommended_movies, recommended_movies_posters

# Function to add a movie to the user's watch history
def add_to_watch_history(user_id, movie_title):
    movie_id = movies[movies['title'] == movie_title].iloc[0].movie_id
    try:
        c.execute("INSERT INTO watch_history (user_id, movie_id) VALUES (?, ?)", (user_id, int(movie_id)))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

# Updated function to handle hybrid recommendation (content + collaborative)
def hybrid_recommend(movie_title, user_id):
    # Get recommended movies and posters from content-based recommendation
    recommended_movies, recommended_movies_posters = recommend(movie_title)
    
    # Fetch additional recommendations based on user history
    additional_recommendations, additional_posters = recommend_based_on_history(user_id)
    
    # Combine content-based and history-based recommendations while keeping titles and posters aligned
    combined_recommendations_with_posters = list(zip(recommended_movies, recommended_movies_posters)) + \
                                            list(zip(additional_recommendations, additional_posters))
    
    # Remove duplicates while preserving the first occurrence
    seen_titles = set()
    unique_recommendations_with_posters = []
    for title, poster in combined_recommendations_with_posters:
        if title not in seen_titles:
            seen_titles.add(title)
            unique_recommendations_with_posters.append((title, poster))
    
    # Extract the final lists of titles and posters to return
    final_recommendations = [title for title, poster in unique_recommendations_with_posters][:5]
    final_posters = [poster for title, poster in unique_recommendations_with_posters][:5]
    
    return final_recommendations, final_posters

# Function to use ChatGPT for movie recommendations
def chatgpt_recommend(query):
    # Step 1: Search for movies in the local movies DataFrame based on the user's query
    matched_movies = movies[movies['title'].str.contains(query, case=False, na=False)]

    # If matching movies are found, display those as recommendations with ChatGPT commentary
    if not matched_movies.empty:
        recommended_details = []

        for index, row in matched_movies.iterrows():
            movie_title = row['title']
            movie_id = row['movie_id']
            poster_url = fetch_poster(movie_id)  # Function to fetch the movie poster

            # Use ChatGPT to provide a recommendation commentary for the specific movie
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "user", "content": f"Briefly explain why '{movie_title}' is a good movie."}
                ],
                max_tokens=50  # Reduced to get concise responses
            )
            chatgpt_commentary = response.choices[0].message['content'].strip()

            recommended_details.append({
                "title": movie_title,
                "poster": poster_url,
                "commentary": chatgpt_commentary
            })

        return recommended_details  # Return the details as a list of dictionaries

    # Step 2: If no matches, use ChatGPT to generate a general movie recommendation
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "user", "content": f"Can you recommend some movies for '{query}'?"}
        ],
        max_tokens=100  # Ensure a reasonable length for general recommendations
    )
    gpt_recommendation = response.choices[0].message['content'].strip()

    # Step 3: Check if the recommended movie exists in the dataset
    matched_movie = movies[movies['title'].str.contains(gpt_recommendation, case=False, na=False)]
    if not matched_movie.empty:
        movie_id = matched_movie.iloc[0]['movie_id']
        poster_url = fetch_poster(movie_id)
        return [{
            "title": gpt_recommendation,
            "poster": poster_url,
            "commentary": "This is a recommended movie based on your query."
        }]

    else:
        return [{
            "title": gpt_recommendation,
            "poster": None,
            "commentary": "Not available in our database. If you'd like to request this movie, let us know!"
        }]
        
# Function to add or update a movie rating
def rate_movie(user_id, movie_title, rating):
    movie_id = movies[movies['title'] == movie_title].iloc[0].movie_id
    try:
        c.execute("INSERT OR REPLACE INTO ratings (user_id, movie_id, rating) VALUES (?, ?, ?)",
                  (user_id, int(movie_id), rating))
        conn.commit()
        return True
    except sqlite3.Error as e:
        st.error(f"Error: {e}")
        return False

# Function to calculate top-ranked movies based on average ratings
def top_ranked_movies():
    query = '''
    SELECT movie_id, AVG(rating) as avg_rating 
    FROM ratings 
    GROUP BY movie_id 
    ORDER BY avg_rating DESC 
    LIMIT 5
    '''
    top_movies = c.execute(query).fetchall()

    top_movie_titles = []
    top_movie_posters = []

    for movie in top_movies:
        movie_id = movie[0]
        avg_rating = movie[1]
        if movie_id in movies['movie_id'].values:
            movie_title = movies[movies['movie_id'] == movie_id]['title'].values[0]
            top_movie_titles.append(f"{movie_title} - {avg_rating:.1f} ⭐")
            top_movie_posters.append(fetch_poster(movie_id))

    return top_movie_titles, top_movie_posters

# Load movie data and similarity matrix
movies_dict = pickle.load(open('D:\\projects\\movie\\notebook\\movie_dict_4.pkl', 'rb'))
movies = pd.DataFrame(movies_dict)
similarity = pickle.load(open('D:\\projects\\movie\\notebook\\similarity_4.pkl', 'rb'))

# Session management
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

# Streamlit UI
st.title("Cineplex - Movie Recommendation System")
st.sidebar.title("Navigation")
page = st.sidebar.selectbox("Choose a page", ["Login", "Movies", "Search", "ChatBot", "Logout"])
if st.session_state.user_id is None:
    st.error("Please login or Register")
elif st.session_state.user_id:
    st.write("Welcome back, " + c.execute("SELECT username FROM users WHERE user_id=?", (st.session_state.user_id,)).fetchone()[0])

# Login/Register functionality
if page == "Login":
    auth_choice = st.selectbox("Login or Register", ["Login", "Register"])
    username = st.text_input("Username")
    password = st.text_input("Password", type='password')

    if auth_choice == "Register" and st.button("Register"):
        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        try:
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            conn.commit()
            st.success("Registration successful!")
        except sqlite3.IntegrityError:
            st.error("Username already exists!")
    elif auth_choice == "Login" and st.button("Login"):
        user = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and bcrypt.checkpw(password.encode('utf-8'), user[2]):
            st.session_state.user_id = user[0]
            st.success("Login successful!")
        else:
            st.error("Invalid credentials")

# Movie recommendations page
elif page == "Movies" and st.session_state.user_id:
    st.write("Welcome to the Movies page!")
    st.write("Your personalized recommendations:")

    names, posters = recommend_based_on_history(st.session_state.user_id)
    if names:
        for name, poster in zip(names, posters):
            st.image(poster, width=150)
            st.write(f"**{name}**")
            # Allow users to rate the movie
            rating = st.slider(f"Rate {name}", 0.0, 5.0, 0.0, step=0.5)
            if st.button(f"Submit rating for {name}"):
                if rate_movie(st.session_state.user_id, name, rating):
                    st.success(f"Rating submitted for {name}!")
    else:
        st.write("No recommendations. Add movies to your watch history.")
        # When a new user registers, show top-rated movies
        st.write("Top-rated movies by all users:")
        top_titles, top_posters = top_ranked_movies()
        for title, poster in zip(top_titles, top_posters):
            st.image(poster, width=150)
            st.write(title)

# Search recommendations page
elif page == "Search" and st.session_state.user_id:
    st.write("Search for movie recommendations:")
    selected_movie = st.selectbox('Search and add a movie to your watch history:', movies['title'].values)
    if st.button("Add to Watch History"):
        if add_to_watch_history(st.session_state.user_id, selected_movie):
            st.success(f"{selected_movie} added!")
        else:
            st.error(f"{selected_movie} is already in your watch history.")
    if st.button("Search"):
        names, posters = hybrid_recommend(selected_movie, st.session_state.user_id)
        for name, poster in zip(names, posters):
            st.image(poster, width=150)
            st.write(f"**{name}**")

# ChatBot page
elif page == "ChatBot" and st.session_state.user_id:
    st.write("Chat with our movie recommendation bot!")
    user_query = st.text_input("Ask for a movie recommendation:")
    
    if st.button("Ask"):
        # Get the recommendations from the chatgpt_recommend function
        chatgpt_recommendations = chatgpt_recommend(user_query)

        # Display each recommendation
        for recommendation in chatgpt_recommendations:
            st.write(f"{recommendation['title']}")
            if recommendation['poster']:
                st.image(recommendation['poster'], width=200)  # Display poster
            st.write(f"ChatGPT says: {recommendation['commentary']}")
            
# Logout page
elif page == "Logout" and st.session_state.user_id:
    st.session_state.user_id = None
    st.success("You have been logged out.")
    st.write("You are now logged out. Thank you for using Cineplex!")
