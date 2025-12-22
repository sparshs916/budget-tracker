from flask import Flask, render_template_string
from config import *
app = Flask(__name__)


HTML_PAGE = f"""
<!DOCTYPE html>
<html>
  <body>
    <h1>Add a New Bank to SparshBerryPi</h1>
    <button id="teller-connect">Connect New Bank</button>

    <script src="https://cdn.teller.io/connect/connect.js"></script>
    <script>
      var teller = TellerConnect.setup({{
        applicationId: "{APP_ID}",
        onSuccess: function(enrollment) {{
          console.log("New Access Token:", enrollment.accessToken);
          alert("Success! Check your terminal for the token.");
        }},
      }});

      document.getElementById('teller-connect').addEventListener('click', function() {{
        teller.open();
      }});
    </script>
  </body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

if __name__ == '__main__':
    # Run this on your Pi's IP so you can access it from your laptop
    app.run(host='10.0.0.186', port=8080)