# Use the official Python image from the Docker Hub
FROM python:3.9-alpine

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file to the container at /app
COPY requirements.txt .

# Install any dependencies specified in requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . .

# Expose port 5000 for the Flask app
EXPOSE 5100

# Command to run the Flask app
CMD ["flask", "run", "--host=0.0.0.0", "--port=5100"]
