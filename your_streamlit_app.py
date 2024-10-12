import streamlit as st
from googleapiclient.discovery import build
import yt_dlp
from pydub import AudioSegment
import os
import tempfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import re
import zipfile
import logging
import shutil

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load API key and email credentials from .env file
from os import environ as env
api_key = env.get('YOUTUBE_API_KEY')
sender_email = env.get('SENDER_EMAIL')
email_password = env.get('EMAIL_PASSWORD')

# Validate environment variables
if not all([api_key, sender_email, email_password]):
    st.error("Missing environment variables. Please check your .env file.")
    st.stop()

# Determine the number of CPU cores available
num_cores = multiprocessing.cpu_count()

# Function to validate email
def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

# Function to get YouTube links
def get_youtube_links(api_key, query, max_results=20):
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        search_response = youtube.search().list(
            q=query,
            part='snippet',
            type='video',
            maxResults=max_results
        ).execute()

        videos = []
        for item in search_response['items']:
            video_id = item['id']['videoId']
            video_title = item['snippet']['title']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            videos.append((video_title, video_url))

        return videos
    except Exception as e:
        st.error(f"Failed to fetch YouTube links: {e}")
        return []

# Function to download audio from YouTube
def download_single_audio(url, index, download_path):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}_%(title)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        downloaded_files = [f for f in os.listdir(download_path) if f.startswith(f"song_{index}_") and f.endswith(".mp3")]
        if downloaded_files:
            return os.path.join(download_path, downloaded_files[0])
        else:
            st.error(f"Downloaded file not found for {url}")
            return None
    except Exception as e:
        st.error(f"Error downloading audio: {e}")
        return None

# Function to download all audio files in parallel
def download_all_audio(video_urls, download_path):
    downloaded_files = []
    progress_bar = st.progress(0)
    num_videos = len(video_urls)
    with ThreadPoolExecutor(max_workers=num_cores) as executor:
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for i, future in enumerate(as_completed(futures)):
            try:
                mp3_file = future.result()
                if mp3_file:
                    downloaded_files.append(mp3_file)
                progress_bar.progress((i + 1) / num_videos)
            except Exception as e:
                st.error(f"Error occurred: {e}")

    return downloaded_files

# Updated function to create a mashup from audio files
import os

def create_mashup(audio_files, output_file, trim_duration):
    # Initialize an empty mashup
    mashup = AudioSegment.silent(duration=0)

    total_trim_duration_per_file = trim_duration * 1000  # Convert to milliseconds

    logging.info(f"Trim duration for each file: {total_trim_duration_per_file} ms")

    # Process each file
    for file in audio_files:
        try:
            audio = AudioSegment.from_file(file)
            logging.info(f"Original length of {file}: {len(audio)} ms")

            # Trim the audio and append it
            part = audio[:total_trim_duration_per_file]
            logging.info(f"Trimmed part length: {len(part)} ms")
            mashup += part

        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")
            return None

    # Final trimming to the exact expected duration
    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)
    logging.info(f"Mashup length before final trimming: {len(mashup)} ms")
    logging.info(f"Expected mashup length: {expected_mashup_duration} ms")

    mashup = mashup[:expected_mashup_duration]
    logging.info(f"Final mashup length: {len(mashup)} ms")

    # Export the mashup
    mashup.export(output_file, format="mp3", bitrate="128k")

    # Ensure only one final file size log
    final_file_size = os.path.getsize(output_file) / (1024 * 1024)
    logging.info(f"Final mashup file size: {final_file_size:.2f} MB")

    return output_file

    # Initialize an empty mashup
    mashup = AudioSegment.silent(duration=0)

    # Calculate the expected duration for each audio part in milliseconds
    total_trim_duration_per_file = trim_duration * 1000  # Trim duration per file (milliseconds)

    logging.info(f"Trim duration for each file: {total_trim_duration_per_file} ms")

    # Loop through each file and append the trimmed part to the mashup
    for file in audio_files:
        try:
            audio = AudioSegment.from_file(file)

            # Log the original audio length for debugging
            logging.info(f"Original length of {file}: {len(audio)} ms")

            # Trim the audio to the specified duration
            part = audio[:total_trim_duration_per_file]

            # Log the trimmed part length
            logging.info(f"Trimmed part length: {len(part)} ms")

            # Append the trimmed part to the mashup
            mashup += part

        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")
            st.error(f"Error processing file {file}: {e}")

    # Now, calculate the total expected mashup duration
    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)

    # Log the total mashup duration before trimming
    logging.info(f"Mashup length before final trimming: {len(mashup)} ms")
    logging.info(f"Expected mashup length: {expected_mashup_duration} ms")

    # Trim the mashup to the exact expected duration
    mashup = mashup[:expected_mashup_duration]

    # Log the final mashup length
    logging.info(f"Final mashup length: {len(mashup)} ms")

    # Export the final mashup with controlled settings to avoid oversized files
    mashup.export(output_file, format="mp3", bitrate="128k")
    
    logging.info(f"Final mashup file size: {os.path.getsize(output_file) / (1024 * 1024):.2f} MB")

    return output_file



def create_zip_file(file_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    
    # Log zip contents
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for file in zip_ref.infolist():
            logging.info(f"Zip contains: {file.filename}, Size: {file.file_size} bytes")
    
    return zip_path

# Updated send_email function to handle zip files
def send_email(sender_email, receiver_email, subject, body, attachment_path, password):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        body = body or "Your YouTube mashup is attached."
        msg.attach(MIMEText(body, 'plain'))

        if not os.path.exists(attachment_path):
            raise FileNotFoundError(f"Attachment file not found: {attachment_path}")

        with open(attachment_path, 'rb') as attachment:
            part = MIMEBase('application', 'zip')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f"attachment; filename= {os.path.basename(attachment_path)}")
            msg.attach(part)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, password)
        text = msg.as_string()
        server.sendmail(sender_email, receiver_email, text)
        server.quit()

        st.success("Email sent successfully!")
    except FileNotFoundError as e:
        st.error(f"Attachment error: {e}")
    except smtplib.SMTPAuthenticationError:
        st.error("Failed to authenticate with the email server. Please check your email credentials.")
    except Exception as e:
        st.error(f"Failed to send email: {e}")
        st.error(f"Error details: sender_email={sender_email}, receiver_email={receiver_email}, subject={subject}, attachment_path={attachment_path}")

# Main Streamlit app
st.title("YouTube Mashup Creator")

# User inputs
singer_name = st.text_input("Enter singer name")
num_videos = st.number_input("Enter number of videos", min_value=1, max_value=20, value=5)
trim_duration = st.number_input("Enter trim duration for each video (seconds)", min_value=1, max_value=60, value=12)
receiver_email = st.text_input("Enter your email address")

# Create and send mashup button
if st.button("Create and Send Mashup"):
    if singer_name and is_valid_email(receiver_email):
        with st.spinner('Creating and sending mashup...'):
            # Get YouTube links
            videos = get_youtube_links(api_key, singer_name, max_results=num_videos)

            if videos:
                # Download audio
                download_path = tempfile.mkdtemp()
                video_urls = [url for _, url in videos]
                audio_files = download_all_audio(video_urls, download_path)

                # Create mashup
                if audio_files:
                    output_file = os.path.join(tempfile.gettempdir(), "mashup.mp3")
                    mashup_file = create_mashup(audio_files, output_file, trim_duration)

                    # Log the final file size
                    file_size = os.path.getsize(mashup_file) / (1024*1024)  # Convert to MB
                    # logging.info(f"Final mashup file size: {file_size:.2f} MB")

                    # Save a local copy for manual inspection
                    local_copy = "/tmp/mashup_copy.mp3"
                    shutil.copy(mashup_file, local_copy)
                    logging.info(f"Local copy saved at: {local_copy}")

                    # Create zip file
                    zip_file = os.path.join(tempfile.gettempdir(), "mashup.zip")
                    create_zip_file(mashup_file, zip_file)

                    # Send email
                    subject = f"Your {singer_name} YouTube Mashup"
                    body = f"Please find attached your custom YouTube mashup of {singer_name} songs. Duration: {trim_duration * num_videos} seconds."
                    send_email(sender_email, receiver_email, subject, body, zip_file, email_password)

                    # Cleanup
                    if os.path.exists(mashup_file):
                        os.remove(mashup_file)
                    if os.path.exists(zip_file):
                        os.remove(zip_file)
                    for file in audio_files:
                        if os.path.exists(file):
                            os.remove(file)
                else:
                    st.error("Failed to download audio files. Please try again.")
            else:
                st.error(f"No videos found for {singer_name}. Please try a different singer name.")
    else:
        if not singer_name:
            st.error("Please enter a singer name.")
        if not is_valid_email(receiver_email):
            st.error("Please enter a valid email address.")

st.info("This app creates a mashup of songs by your chosen singer and sends it to your email. Enter the singer name, number of videos to use, trim duration for each video, and your email address, then click the button!")