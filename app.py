from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os

app = Flask(__name__)
CORS(app)  # Разрешить кросс-доменные запросы

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/parse', methods=['POST'])
def parse():
    url = request.json['url']

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        r = requests.get(url, headers=headers)
        soup = BeautifulSoup(r.text, 'html.parser')

        title = soup.find('h1', class_='item__title').get_text(strip=True)
        price = soup.find('div', class_='item__price-once').get_text(strip=True)
        img = soup.find('img', class_='item__gallery-img')['src']

        return jsonify({
            'title': title,
            'price': price,
            'img': img
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
