from flask import Flask, request, send_from_directory, render_template_string, jsonify
import os

app = Flask(__name__)

# Ensure uploads folder exists
if not os.path.exists("uploads"):
    os.makedirs("uploads")

# HTML Template
UPLOAD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>QuickShare</title>
</head>
<body>
    <h2>Upload a File</h2>

    <input type="file" id="fileInput">
    <button onclick="uploadFile()">Upload</button>

    <br><br>
    <progress id="progressBar" value="0" max="100" style="width:300px;"></progress>
    <p id="status"></p>

    <h2>Download Files</h2>
    <ul>
        {% for file in files %}
            <li><a href="/download/{{ file }}">{{ file }}</a></li>
        {% endfor %}
    </ul>

<script>
function uploadFile() {
    var file = document.getElementById("fileInput").files[0];

    if (!file) {
        alert("Please select a file first!");
        return;
    }

    var formData = new FormData();
    formData.append("file", file);

    var xhr = new XMLHttpRequest();

    xhr.upload.addEventListener("progress", function(e) {
        if (e.lengthComputable) {
            var percent = Math.round((e.loaded / e.total) * 100);
            document.getElementById("progressBar").value = percent;
            document.getElementById("status").innerHTML = percent + "% uploaded...";
        }
    });

    xhr.onload = function() {
        if (xhr.status == 200) {
            document.getElementById("status").innerHTML = "Upload complete!";
            setTimeout(() => location.reload(), 1000);
        } else {
            document.getElementById("status").innerHTML = "Upload failed!";
            console.log(xhr.responseText);
        }
    };

    xhr.onerror = function() {
        document.getElementById("status").innerHTML = "Error occurred!";
    };

    xhr.open("POST", "/upload", true);
    xhr.send(formData);
}
</script>

</body>
</html>
"""

@app.route('/')
def index():
    files = os.listdir("uploads")
    return render_template_string(UPLOAD_HTML, files=files)


@app.route('/upload', methods=['POST'])
def upload_file():
    print("FILES RECEIVED:", request.files)  # Debug

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    filepath = os.path.join("uploads", file.filename)
    file.save(filepath)

    return jsonify({"message": f"{file.filename} uploaded successfully"}), 200


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory("uploads", filename, as_attachment=True)


if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=5000)