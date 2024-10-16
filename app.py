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
from flask import Flask, render_template, request, jsonify, send_from_directory
from googleapiclient.discovery import build
import yt_dlp
from pydub import AudioSegment

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, static_folder='static')


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
        logging.error(f"Failed to fetch YouTube links: {e}")
        return []

# Function to download audio from YouTube
def download_single_audio(url, index, download_path):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'retries': 10,
        'fragment_retries': 10,
        'ignoreerrors': True,
        'no_warnings': True,
        'quiet': True,
        'no_color': True,
    }

    max_attempts = 5
    base_delay = 5
    for attempt in range(max_attempts):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            downloaded_files = [f for f in os.listdir(download_path) if f.startswith(f"song_{index}.") and f.endswith(".mp3")]
            if downloaded_files:
                return os.path.join(download_path, downloaded_files[0])
            else:
                logging.error(f"Downloaded file not found for {url}")
                return None
        except Exception as e:
            logging.error(f"Error downloading audio (attempt {attempt + 1}/{max_attempts}): {e}")
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            logging.info(f"Waiting for {delay:.2f} seconds before retrying...")
            time.sleep(delay)
    
    logging.error(f"Failed to download audio after {max_attempts} attempts: {url}")
    return None


# Function to download all audio files in parallel
def download_all_audio(video_urls, download_path):
    downloaded_files = []
    with ThreadPoolExecutor(max_workers=num_cores) as executor:
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for future in as_completed(futures):
            try:
                mp3_file = future.result()
                if mp3_file:
                    downloaded_files.append(mp3_file)
            except Exception as e:
                logging.error(f"Error occurred: {e}")

    return downloaded_files

# Function to create a mashup from audio files
def create_mashup(audio_files, output_file, trim_duration):
    mashup = AudioSegment.silent(duration=0)
    total_trim_duration_per_file = trim_duration * 1000  # Convert to milliseconds

    for file in audio_files:
        try:
            audio = AudioSegment.from_file(file)
            if len(audio) < total_trim_duration_per_file:
                logging.warning(f"Audio file {file} is shorter than trim duration. Using full length.")
                part = audio
            else:
                part = audio[:total_trim_duration_per_file]
            mashup += part
        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")

    if len(mashup) == 0:
        logging.error("No audio files were successfully processed.")
        return None

    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)
    if len(mashup) < expected_mashup_duration:
        logging.warning(f"Mashup duration ({len(mashup)}ms) is less than expected ({expected_mashup_duration}ms).")
    else:
        mashup = mashup[:expected_mashup_duration]

    mashup.export(output_file, format="mp3", bitrate="128k")
    return output_file

def create_zip_file(file_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    return zip_path

# Function to send email
def send_email(sender_email, receiver_email, subject, body, attachment_path, password):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

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

        logging.info("Email sent successfully!")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {request.url}")
    return jsonify(error=str(e)), 404

@app.errorhandler(500)
def internal_server_error(e):
    logging.error(f"500 error: {str(e)}")
    return jsonify(error="Internal Server Error"), 500

@app.route('/create_mashup', methods=['POST'])
def create_mashup_route():
    try:
        # Log incoming request data
        logging.info(f"Received create_mashup request: {request.form}")

        # Validate and extract form data
        singer_name = request.form.get('singer_name')
        num_videos = request.form.get('num_videos')
        trim_duration = request.form.get('trim_duration')
        receiver_email = request.form.get('receiver_email')

        # Validate required fields
        if not all([singer_name, num_videos, trim_duration, receiver_email]):
            missing_fields = [field for field in ['singer_name', 'num_videos', 'trim_duration', 'receiver_email'] if not request.form.get(field)]
            logging.error(f"Missing required fields: {missing_fields}")
            return jsonify({'status': 'error', 'message': f'Missing required fields: {", ".join(missing_fields)}'})

        # Validate and convert numeric fields
        try:
            num_videos = max(int(num_videos), 10)
            trim_duration = max(int(trim_duration), 20)
        except ValueError as e:
            logging.error(f"Invalid numeric values: {str(e)}")
            return jsonify({'status': 'error', 'message': 'Invalid numeric values for num_videos or trim_duration'})

        # Validate email
        if not is_valid_email(receiver_email):
            logging.error(f"Invalid email address: {receiver_email}")
            return jsonify({'status': 'error', 'message': 'Please enter a valid email address.'})

        # Fetch YouTube links
        logging.info(f"Fetching YouTube links for {singer_name}")
        videos = get_youtube_links(api_key, singer_name, max_results=num_videos)

        if not videos:
            logging.warning(f"No videos found for {singer_name}")
            return jsonify({'status': 'error', 'message': f'No videos found for {singer_name}. Please try a different singer name.'})

        # Create temporary directory
        with tempfile.TemporaryDirectory() as download_path:
            logging.info(f"Created temporary directory: {download_path}")

            # Download audio files
            video_urls = [url for _, url in videos]
            logging.info(f"Downloading {len(video_urls)} audio files")
            audio_files = download_all_audio(video_urls, download_path)

            if not audio_files:
                logging.error("Failed to download audio files")
                return jsonify({'status': 'error', 'message': 'Failed to download audio files. Please try again.'})

            # Create mashup
            logging.info("Creating mashup")
            output_file = os.path.join(download_path, "mashup.mp3")
            mashup_file = create_mashup(audio_files, output_file, trim_duration)

            if not mashup_file:
                logging.error("Failed to create mashup")
                return jsonify({'status': 'error', 'message': 'Failed to create mashup. Please try again.'})

            # Create zip file
            zip_file = os.path.join(download_path, "mashup.zip")
            create_zip_file(mashup_file, zip_file)

            # Send email
            logging.info(f"Sending email to {receiver_email}")
            subject = f"Your {singer_name} YouTube Mashup"
            body = f"Please find attached your custom YouTube mashup of {singer_name} songs. Duration: {trim_duration * len(audio_files)} seconds."
            email_sent = send_email(sender_email, receiver_email, subject, body, zip_file, email_password)

            if email_sent:
                logging.info("Mashup created and sent successfully")
                return jsonify({'status': 'success', 'message': 'Mashup created and sent successfully! Check your email.'})
            else:
                logging.error("Failed to send email")
                return jsonify({'status': 'error', 'message': 'Mashup created but failed to send email. Please try again.'})

    except Exception as e:
        logging.error(f"Unexpected error in create_mashup_route: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': f'An unexpected error occurred: {str(e)}'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7100))
    app.run(host='0.0.0.0', port=port, debug=True)
