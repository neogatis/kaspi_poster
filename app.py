from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os

app = Flask(__name__)
CORS(app)  # Разрешить запросы с HTML-страниц

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

        # Название товара
        title_tag = soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else None

        # Цена товара
        price_tag = soup.select_one('[data-test-id="product-price"]')
        price = price_tag.get_text(strip=True) if price_tag else None

        # Картинка товара
        img_tag = soup.select_one('picture img')
        img = img_tag['src'] if img_tag else None

        if not title or not price or not img:
            return jsonify({
                'error': 'Не удалось получить данные. Kaspi мог изменить структуру страницы.'
            }), 500

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
