import os
import tempfile
import logging
import re
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, stream_with_context
from googleapiclient.discovery import build
import yt_dlp
import soundfile as sf
import numpy as np
import time
import random
from moviepy.editor import VideoFileClip
import queue
import threading

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
app = Flask(__name__)

# Global queue for log messages
log_queue = queue.Queue()

# Load environment variables
load_dotenv()
api_key = os.getenv('YOUTUBE_API_KEY')
sender_email = os.getenv('SENDER_EMAIL')
email_password = os.getenv('EMAIL_PASSWORD')

# Validate environment variables
if not all([api_key, sender_email, email_password]):
    logging.error("Missing environment variables. Please check your .env file.")
    raise ValueError("Missing environment variables. Please check your .env file.")

# Determine the number of CPU cores available
num_cores = multiprocessing.cpu_count()

def is_valid_email(email):
    """Validate email format."""
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

def get_youtube_links(api_key, query, max_results=20):
    """Fetch YouTube video links based on search query."""
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
        logging.error(f"Failed to fetch YouTube links: {e}")
        return []

def download_single_audio(url, index, download_path):
    """Download audio from a single YouTube video using improved method."""
    log_queue.put(f"DOWNLOADING video {index} from {url}")
    
    ydl_opts = {
        'format': 'bestvideo[height<=480]+bestaudio/best',
        'outtmpl': f'{download_path}/video_{index}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }

    max_attempts = 5
    base_delay = 5
    
    for attempt in range(max_attempts):
        try:
            # Add random delay to avoid rate limiting
            time.sleep(random.uniform(1, 5))
            
            # Download the video first
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            # Find the downloaded video file
            downloaded_files = [f for f in os.listdir(download_path) 
                              if f.startswith(f"video_{index}.")]
            
            if not downloaded_files:
                log_queue.put(f"Downloaded file not found for {url}")
                continue
                
            video_path = os.path.join(download_path, downloaded_files[0])
            
            # Convert to audio using VideoFileClip
            try:
                video = VideoFileClip(video_path)
                audio_file = os.path.join(download_path, f'song_{index}.wav')
                video.audio.write_audiofile(audio_file, 
                                         codec='wav',
                                         ffmpeg_params=["-loglevel", "quiet"])
                video.close()
                
                # Clean up video file
                os.remove(video_path)
                
                log_queue.put(f"Successfully downloaded and converted video {index}")
                return audio_file
                
            except Exception as e:
                log_queue.put(f"Error converting video to audio: {e}")
                if os.path.exists(video_path):
                    os.remove(video_path)
                raise
                
        except Exception as e:
            log_queue.put(f"Error downloading audio (attempt {attempt + 1}/{max_attempts}): {e}")
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            log_queue.put(f"Waiting for {delay:.2f} seconds before retrying...")
            time.sleep(delay)
    
    log_queue.put(f"Failed to download audio after {max_attempts} attempts: {url}")
    return None

def download_all_audio(video_urls, download_path):
    """Download all audio files in parallel using improved method."""
    downloaded_files = []
    
    # Create download directory if it doesn't exist
    os.makedirs(download_path, exist_ok=True)
    
    # Clean existing files in download directory
    for f in os.listdir(download_path):
        try:
            os.remove(os.path.join(download_path, f))
        except Exception as e:
            log_queue.put(f"Error cleaning up file {f}: {e}")
    
    with ThreadPoolExecutor(max_workers=num_cores) as executor:
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for future in as_completed(futures):
            try:
                audio_file = future.result()
                if audio_file and os.path.exists(audio_file):
                    downloaded_files.append(audio_file)
                    log_queue.put(f"Successfully processed {audio_file}")
            except Exception as e:
                log_queue.put(f"Error occurred during download: {e}")

    log_queue.put(f"Successfully downloaded {len(downloaded_files)} audio files")
    return downloaded_files

def create_mashup(audio_files, output_file, trim_duration):
    """Create a mashup from multiple audio files."""
    log_queue.put("GENERATING Mashup file")
    mashup = None
    total_trim_duration_per_file = trim_duration  # In seconds
    samplerate = None

    for file in audio_files:
        try:
            audio, file_samplerate = sf.read(file)
            
            # Set samplerate if not set yet
            if samplerate is None:
                samplerate = file_samplerate

            # Convert to mono if stereo
            if audio.ndim > 1:
                audio = np.mean(audio, axis=1)

            # Trim audio to specified duration
            if len(audio) / samplerate < total_trim_duration_per_file:
                log_queue.put(f"Audio file {file} is shorter than trim duration. Using full length.")
                part = audio
            else:
                part = audio[:int(total_trim_duration_per_file * samplerate)]

            # Add to mashup
            if mashup is None:
                mashup = part
            else:
                mashup = np.concatenate((mashup, part))

        except Exception as e:
            log_queue.put(f"Error processing file {file}: {e}")

    if mashup is None or len(mashup) == 0:
        log_queue.put("No audio files were successfully processed.")
        return None

    # Ensure final mashup duration matches expected length
    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)
    if len(mashup) / samplerate < expected_mashup_duration:
        log_queue.put(f"Mashup duration ({len(mashup)/samplerate}s) is less than expected ({expected_mashup_duration}s).")
    else:
        mashup = mashup[:int(expected_mashup_duration * samplerate)]

    sf.write(output_file, mashup, samplerate)
    return output_file

def create_zip_file(file_path, zip_path):
    """Create a ZIP file containing the mashup."""
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    return zip_path

def send_email(sender_email, receiver_email, subject, body, attachment_path, password):
    """Send email with attachment using Gmail SMTP."""
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        # Add attachment
        with open(attachment_path, 'rb') as attachment:
            part = MIMEBase('application', 'zip')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 
                          f"attachment; filename= {os.path.basename(attachment_path)}")
            msg.attach(part)

        # Connect to Gmail SMTP server
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()  # Enable TLS
        
        # Login to Gmail
        try:
            server.login(sender_email, password)
        except smtplib.SMTPAuthenticationError:
            log_queue.put("Authentication failed. Please check your email and password.")
            return False

        # Send email
        text = msg.as_string()
        server.sendmail(sender_email, receiver_email, text)
        server.quit()

        log_queue.put("Email sent successfully!")
        return True
    except Exception as e:
        log_queue.put(f"Failed to send email: {e}")
        return False

def create_mashup_process(singer_name, num_videos, trim_duration, receiver_email):
    """Main process for creating and sending mashup."""
    try:
        log_queue.put("Starting mashup creation process...")

        # Get YouTube videos
        log_queue.put(f"Fetching YouTube links for {singer_name}")
        videos = get_youtube_links(api_key, f"{singer_name} official new video song", max_results=num_videos)

        if not videos:
            log_queue.put(f"No videos found for {singer_name}")
            return

        video_urls = [url for _, url in videos]

        # Create temporary directory for processing
        with tempfile.TemporaryDirectory() as download_path:
            log_queue.put(f"Created temporary directory: {download_path}")

            # Download audio files
            log_queue.put(f"Downloading {len(video_urls)} audio files")
            audio_files = download_all_audio(video_urls, download_path)

            if not audio_files:
                log_queue.put("Failed to download audio files")
                return

            # Create mashup
            log_queue.put("Creating mashup")
            output_file = os.path.join(download_path, "mashup.wav")
            mashup_file = create_mashup(audio_files, output_file, trim_duration)

            if not mashup_file:
                log_queue.put("Failed to create mashup")
                return

            # Create ZIP file
            zip_file = os.path.join(download_path, "mashup.zip")
            create_zip_file(mashup_file, zip_file)

            # Send email
            log_queue.put(f"Sending email to {receiver_email}")
            subject = f"Your {singer_name} YouTube Mashup"
            body = f"""
            Hello!
            
            Your custom YouTube mashup of {singer_name} songs is ready! 
            Duration: {trim_duration * len(audio_files)} seconds.
            
            Please find the mashup attached to this email.
            
            Best regards,
            Your Mashup App
            """
            email_sent = send_email(sender_email, receiver_email, subject, body, zip_file, email_password)

            if email_sent:
                log_queue.put("Mashup created and sent successfully!")
            else:
                log_queue.put("Failed to send email")

    except Exception as e:
        log_queue.put(f"Error occurred: {str(e)}")

# Flask routes
@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')

@app.route('/create_mashup', methods=['POST'])
def create_mashup_route():
    """Handle mashup creation request."""
    try:
        # Get form data
        singer_name = request.form.get('singer_name')
        num_videos = int(request.form.get('num_videos', 10))
        trim_duration = int(request.form.get('trim_duration', 20))
        receiver_email = request.form.get('receiver_email')

        # Validate inputs
        if not all([singer_name, receiver_email]):
            return jsonify({
                'status': 'error',
                'message': 'Missing required fields'
            })

        if not is_valid_email(receiver_email):
            return jsonify({
                'status': 'error',
                'message': 'Invalid email address'
            })

        # Start mashup process in background
        threading.Thread(
            target=create_mashup_process,
            args=(singer_name, num_videos, trim_duration, receiver_email)
        ).start()

        return jsonify({
            'status': 'success',
            'message': 'Mashup creation process started. Please check the logs for progress.'
        })

    except Exception as e:
        log_queue.put(f"Error in create_mashup_route: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'An error occurred: {str(e)}'
        })

@app.route('/logs')
def logs():
    """Stream logs to client."""
    def generate():
        while True:
            log_message = log_queue.get()
            yield f"data: {log_message}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream'
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5900))
    app.run(host='0.0.0.0', port=port, debug=True)
