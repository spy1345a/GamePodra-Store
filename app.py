import os
import razorpay
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 'yes')

app.config['RAZORPAY_KEY_ID'] = os.getenv('RAZORPAY_KEY_ID')
app.config['RAZORPAY_KEY_SECRET'] = os.getenv('RAZORPAY_KEY_SECRET')

DATABASE_URL = os.getenv('DATABASE_URL')
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

client = razorpay.Client(auth=(app.config['RAZORPAY_KEY_ID'], app.config['RAZORPAY_KEY_SECRET']))

from models import Base, Payment
Base.metadata.create_all(engine)

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
        'payment_capture': 1,
        'notes': {
            'minecraft_name': data.get('minecraft_name', ''),
            'discord_tag': data.get('discord_tag', ''),
            'email': data.get('email', ''),
            'rank': data.get('rank', ''),
            'billing': data.get('billing', '')
        }
    }
    
    try:
        session = Session()
        order = client.order.create(data=order_data)
        
        payment_record = Payment(
            order_id=order['id'],
            minecraft_name=data.get('minecraft_name', ''),
            discord_tag=data.get('discord_tag', ''),
            email=data.get('email', ''),
            rank=data.get('rank', ''),
            rank_key=data.get('rank_key', ''),
            billing=data.get('billing', ''),
            amount=float(data.get('amount', 0)),
            status='pending'
        )
        session.add(payment_record)
        session.commit()
        session.close()
        
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


@app.route('/verify-payment', methods=['POST'])
def verify_payment():
    data = request.get_json()
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_order_id = data.get('razorpay_order_id')
    razorpay_signature = data.get('razorpay_signature')
    
    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        return jsonify({'success': False, 'error': 'Missing payment details'})
    
    try:
        client.utility.verify_payment_signature({
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_order_id': razorpay_order_id,
            'razorpay_signature': razorpay_signature
        })
        
        session = Session()
        payment = session.query(Payment).filter_by(order_id=razorpay_order_id).first()
        if payment:
            payment.payment_id = razorpay_payment_id
            payment.status = 'completed'
            payment.verified_at = datetime.utcnow()
            session.commit()
        session.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/payment-success')
def payment_success():
    return render_template('payment-success.html',
                         rank_name=request.args.get('rank_name', 'N/A'),
                         billing=request.args.get('billing', 'monthly'),
                         price=request.args.get('price', '0'),
                         payment_id=request.args.get('payment_id', ''))


@app.route('/payment-failed')
def payment_failed():
    return render_template('payment-failed.html',
                         rank_name=request.args.get('rank_name', 'N/A'),
                         rank_key=request.args.get('rank_key', 'iron'),
                         billing=request.args.get('billing', 'monthly'),
                         price=request.args.get('price', '0'),
                         error=request.args.get('error', 'Payment was not completed'))


if __name__ == '__main__':
    app.run(debug=app.config['DEBUG'],host="0.0.0.0", port=5000)