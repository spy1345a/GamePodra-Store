import os
import razorpay
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

app.config['RAZORPAY_KEY_ID'] = os.getenv('RAZORPAY_KEY_ID', 'rzp_test_Sr9xDVlfuAWxbm')
app.config['RAZORPAY_KEY_SECRET'] = os.getenv('RAZORPAY_KEY_SECRET', 'kACl7SYPcnTkitHbXsPzD0gG')

client = razorpay.Client(auth=(app.config['RAZORPAY_KEY_ID'], app.config['RAZORPAY_KEY_SECRET']))

RANKS = {
    'iron': {'name': 'IRON', 'monthly': 59, 'lifetime': 399},
    'gold': {'name': 'GOLD', 'monthly': 99, 'lifetime': 699},
    'diamond': {'name': 'DIAMOND', 'monthly': 179, 'lifetime': 1299},
    'netherite': {'name': 'NETHERITE', 'monthly': 299, 'lifetime': 2199},
    'god': {'name': 'GOD', 'monthly': 499, 'lifetime': 3599}
}


@app.route('/')
def index():
    return render_template('index.html', ranks=RANKS)


@app.route('/checkout')
def checkout():
    rank_key = request.args.get('rank', 'iron')
    billing = request.args.get('billing', 'monthly')
    
    rank = RANKS.get(rank_key, RANKS['iron'])
    price = rank[billing]
    
    return render_template('checkout.html', 
                         rank_name=rank['name'],
                         rank_key=rank_key,
                         billing=billing,
                         price=price,
                         razorpay_key_id=app.config['RAZORPAY_KEY_ID'])


@app.route('/create-order', methods=['POST'])
def create_order():
    data = request.get_json()
    amount = int(data.get('amount', 0)) * 100
    
    order_data = {
        'amount': amount,
        'currency': 'INR',
        'payment_capture': 1
    }
    
    try:
        order = client.order.create(data=order_data)
        return jsonify({
            'success': True,
            'order_id': order['id'],
            'razorpay_key_id': app.config['RAZORPAY_KEY_ID']
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/discord-help')
def discord_help():
    return render_template('discord-help.html')


@app.route('/tnc')
def tnc():
    return render_template('tnc.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


if __name__ == '__main__':
    app.run(debug=True)