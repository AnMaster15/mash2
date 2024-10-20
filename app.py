import os
import tempfile
import logging
import re
import zipfile
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import yt_dlp
import soundfile as sf
import numpy as np
import scipy.signal
import time
import random
from moviepy.editor import VideoFileClip
import queue
import threading

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Global queue for log messages
log_queue = queue.Queue()

def send_log(message, level="info"):
    """Send a formatted log message to the client."""
    log_data = {
        "type": "log",
        "level": level,
        "message": message,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    log_queue.put(json.dumps(log_data))
    if level == "error":
        logger.error(message)
    else:
        logger.info(message)

# Load environment variables
load_dotenv()
sender_email = os.getenv('SENDER_EMAIL')
email_password = os.getenv('EMAIL_PASSWORD')
smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
smtp_port = int(os.getenv('SMTP_PORT', '587'))

# Validate environment variables
if not all([sender_email, email_password]):
    error_msg = "Missing environment variables. Please check your .env file."
    logger.error(error_msg)
    raise ValueError(error_msg)

def create_zip_with_mp3(mashup_wav_path, temp_dir):
    """Convert WAV to MP3 and create ZIP archive."""
    try:
        send_log("Converting WAV to MP3")
        base_name = os.path.splitext(os.path.basename(mashup_wav_path))[0]
        mp3_path = os.path.join(temp_dir, f"{base_name}.mp3")
        
        # Load WAV file
        data, sr = sf.read(mashup_wav_path)
        
        # Create MP3 using scipy and numpy for processing
        # Note: Using soundfile to write as MP3 (requires libsndfile with MP3 support)
        sf.write(mp3_path, data, sr, format='mp3')
        
        # Create ZIP file
        zip_path = os.path.join(temp_dir, f"{base_name}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(mp3_path, os.path.basename(mp3_path))
        
        send_log("Successfully created ZIP file with MP3")
        return zip_path
    except Exception as e:
        error_msg = f"Error creating ZIP with MP3: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return None


def send_email_with_attachment(receiver_email, subject, body, attachment_path):
    """Send an email with a ZIP attachment."""
    try:
        send_log(f"Preparing to send email to {receiver_email}")
        
        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = receiver_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        # Attach ZIP file
        with open(attachment_path, "rb") as attachment:
            part = MIMEBase("application", "zip")
            part.set_payload(attachment.read())
            
        encoders.encode_base64(part)
        filename = os.path.basename(attachment_path)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename= {filename}",
        )
        message.attach(part)

        # Send email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, email_password)
            server.send_message(message)

        send_log(f"Email sent successfully to {receiver_email}", "success")
        return True

    except Exception as e:
        error_msg = f"Failed to send email: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return False


def is_valid_email(email):
    """Validate email format."""
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

def get_youtube_links(query, max_results=20):
    """Fetch YouTube video links using yt-dlp."""
    send_log(f"Searching for YouTube videos: {query}")
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'default_search': 'ytsearch',
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f'ytsearch{max_results}:{query}'
            send_log(f"Executing search query: {search_query}")
            results = ydl.extract_info(search_query, download=False)
            
            videos = []
            if 'entries' in results:
                for entry in results['entries']:
                    if entry:
                        video_title = entry.get('title', '')
                        video_url = entry.get('url', '')
                        if video_title and video_url:
                            videos.append((video_title, video_url))
                            send_log(f"Found video: {video_title}")

        send_log(f"Successfully found {len(videos)} videos")
        return videos
    except Exception as e:
        error_msg = f"Failed to fetch YouTube links: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return []

def download_single_audio(url, index, download_path):
    """Download audio from a single YouTube video."""
    send_log(f"Starting download of video {index}: {url}")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            send_log(f"Downloading video {index}")
            info = ydl.extract_info(url, download=True)
            send_log(f"Successfully downloaded video {index}: {info.get('title', 'Unknown')}")

            # Verify the downloaded file
            downloaded_files = [f for f in os.listdir(download_path) 
                              if f.startswith(f"song_{index}") and f.endswith('.wav')]
            
            if not downloaded_files:
                raise FileNotFoundError(f"Downloaded file not found for video {index}")
                
            audio_file = os.path.join(download_path, downloaded_files[0])
            
            # Verify audio file integrity
            with sf.SoundFile(audio_file) as sf_file:
                duration = len(sf_file) / sf_file.samplerate
                send_log(f"Video {index} audio verified - Duration: {duration:.2f}s")
            
            return audio_file

    except Exception as e:
        error_msg = f"Error downloading video {index}: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return None

def create_mashup(audio_files, output_file, trim_duration):
    """Create a mashup from multiple audio files."""
    send_log("Starting mashup creation")
    try:
        processed_audio = []
        target_sr = 44100  # Target sample rate

        for idx, file in enumerate(audio_files, 1):
            send_log(f"Processing audio file {idx}/{len(audio_files)}")
            
            # Load audio file
            audio, sr = sf.read(file)
            send_log(f"Loaded audio file {idx} - Sample rate: {sr}Hz")

            # Convert to mono if stereo
            if len(audio.shape) > 1:
                audio = np.mean(audio, axis=1)
                send_log(f"Converted audio {idx} to mono")

            # Resample if necessary
            if sr != target_sr:
                send_log(f"Resampling audio {idx} to {target_sr}Hz")
                audio = scipy.signal.resample(audio, int(len(audio) * target_sr / sr))

            # Trim audio
            samples_to_keep = min(len(audio), int(trim_duration * target_sr))
            audio = audio[:samples_to_keep]
            send_log(f"Trimmed audio {idx} to {trim_duration} seconds")

            # Normalize audio
            if np.max(np.abs(audio)) > 0:
                audio = audio / np.max(np.abs(audio))
            
            processed_audio.append(audio)

        # Concatenate all processed audio
        send_log("Concatenating processed audio files")
        final_audio = np.concatenate(processed_audio)

        # Apply fade in/out
        fade_duration = min(1.0, trim_duration * 0.1)
        fade_samples = int(fade_duration * target_sr)
        fade_in = np.linspace(0, 1, fade_samples)
        fade_out = np.linspace(1, 0, fade_samples)
        
        final_audio[:fade_samples] *= fade_in
        final_audio[-fade_samples:] *= fade_out
        
        send_log("Applied fade in/out effects")

        # Save final mashup
        sf.write(output_file, final_audio, target_sr)
        send_log(f"Successfully saved mashup to {output_file}")
        
        return output_file

    except Exception as e:
        error_msg = f"Error creating mashup: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return None

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

        if not is_valid_email(receiver_email):
            return jsonify({"status": "error", "message": "Invalid email address"})

        send_log(f"Received mashup request - Singer: {singer_name}, Videos: {num_videos}, Duration: {trim_duration}s")

        with tempfile.TemporaryDirectory() as temp_dir:
            send_log(f"Created temporary directory: {temp_dir}")

            # Get YouTube videos and download audio files
            videos = get_youtube_links(f"{singer_name} song", num_videos)
            if not videos:
                return jsonify({"status": "error", "message": "No videos found"})

            audio_files = []
            for idx, (title, url) in enumerate(videos, 1):
                audio_file = download_single_audio(url, idx, temp_dir)
                if audio_file:
                    audio_files.append(audio_file)

            if not audio_files:
                return jsonify({"status": "error", "message": "Failed to download any audio files"})

            # Create mashup
            output_wav = os.path.join(temp_dir, f"{singer_name}_mashup.wav")
            mashup_file = create_mashup(audio_files, output_wav, trim_duration)

            if mashup_file:
                # Create ZIP with MP3
                zip_file = create_zip_with_mp3(mashup_file, temp_dir)
                if not zip_file:
                    return jsonify({"status": "error", "message": "Failed to create ZIP file"})

                # Send email with ZIP
                subject = f"Your {singer_name} Mashup is Ready!"
                body = f"""
Hello!

Your mashup of {singer_name}'s songs has been created successfully. 
The mashup contains {len(audio_files)} songs, each trimmed to {trim_duration} seconds.
The MP3 file is included in the ZIP attachment.

Enjoy your music!

Best regards,
Your Mashup Creator
"""
                if send_email_with_attachment(receiver_email, subject, body, zip_file):
                    return jsonify({
                        "status": "success",
                        "message": f"Mashup created and sent to {receiver_email}"
                    })
                else:
                    return jsonify({
                        "status": "error",
                        "message": "Mashup created but failed to send email"
                    })
            else:
                return jsonify({"status": "error", "message": "Failed to create mashup"})

    except Exception as e:
        error_msg = f"Error in mashup creation: {str(e)}"
        send_log(error_msg, "error")
        logger.exception(error_msg)
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5800))
    app.run(host='0.0.0.0', port=port, debug=True)
