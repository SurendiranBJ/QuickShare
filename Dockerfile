FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt
RUN apt update
RUN apt install pip
RUN pip install -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python","Quickshare.py"]
