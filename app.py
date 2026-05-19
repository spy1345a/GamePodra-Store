from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

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
                         price=price)


if __name__ == '__main__':
    app.run(debug=True)