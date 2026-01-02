from flask import Flask, render_template, request, redirect, url_for, session, flash
import mysql.connector
from mysql.connector import Error
import datetime
import functools
import os

app = Flask(__name__)

# ΑΣΦΑΛΕΙΑ: Παίρνει το κλειδί από το Cloud, αλλιώς χρησιμοποιεί ένα σταθερό για τοπικά
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_12345')

def get_db_connection():
    """Establishes a secure connection to the database using environment variables."""
    try:
        connection = mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', 'your_local_password'), 
            database=os.environ.get('DB_NAME', 'BankDB'),
            port=int(os.environ.get('DB_PORT', 3306)),
            # Απαραίτητο για σύνδεση με Aiven/Cloud Databases
            ssl_disabled=False 
        )
        return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
        return None

def login_required(view):
    """Decorator to ensure user is logged in."""
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(**kwargs)
    return wrapped_view

@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        tin = request.form['tin']
        
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            # Χρήση Prepared Statement για προστασία από SQL Injection
            cursor.execute("SELECT * FROM customer WHERE TIN = %s", (tin,))
            user = cursor.fetchone()
            cursor.close()
            conn.close()

            if user:
                session['user_id'] = user['CustomerID']
                session['user_name'] = user['Name']
                session['tin'] = user['TIN']
                flash('Login successful!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid TIN. Please try again.', 'danger')
        else:
            flash('Database connection failed.', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    conn = get_db_connection()
    
    # Αρχικοποίηση λιστών για αποφυγή UnboundLocalError
    accounts = []
    debit_cards = []
    credit_cards = []
    loans = []
    recent_transactions = []
    total_assets = 0.0
    total_liabilities = 0.0
    
    if conn:
        cursor = conn.cursor(dictionary=True)
        
        # 1. Λογαριασμοί
        query_acc = """
            SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance,
            CASE WHEN sa.AccountID IS NOT NULL THEN 'Savings' WHEN cha.AccountID IS NOT NULL THEN 'Checking' ELSE 'General' END as AccountType
            FROM customer_accounts ca
            JOIN account acc_table ON ca.AccountID = acc_table.AccountID
            LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID
            LEFT JOIN savings_account sa ON ca.AccountID = sa.AccountID
            LEFT JOIN checking_account cha ON ca.AccountID = cha.AccountID
            WHERE ca.CustomerID = %s
        """
        cursor.execute(query_acc, (user_id,))
        accounts = cursor.fetchall()
        for acc in accounts:
            total_assets += float(acc['Balance'])

        # 2. Χρεωστικές Κάρτες
        query_dc = """
            SELECT dc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV, a.AccountNumber
            FROM debit_card dc
            JOIN card c ON dc.CardID = c.CardID
            JOIN account a ON dc.AccountID = a.AccountID
            WHERE a.CustomerID = %s AND c.Status = 'Active'
        """
        cursor.execute(query_dc, (user_id,))
        debit_cards = cursor.fetchall()

        # 3. Πιστωτικές Κάρτες
        query_cc = """
            SELECT cc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV, cc.CreditLimit, ccb.AvailableBalance,
            (cc.CreditLimit - COALESCE(ccb.AvailableBalance, cc.CreditLimit)) as CurrentDebt
            FROM credit_card cc
            JOIN card c ON cc.CardID = c.CardID
            LEFT JOIN credit_card_balance ccb ON cc.CardID = ccb.CardID
            WHERE cc.CustomerID = %s AND c.Status = 'Active'
        """
        cursor.execute(query_cc, (user_id,))
        credit_cards = cursor.fetchall()

        # 4. Δάνεια
        query_loans = """
            SELECT LoanID, Type, Amount, ExpirationDate, Debt FROM loan_debts
            WHERE CustomerID = %s AND Debt > 0
        """
        cursor.execute(query_loans, (user_id,))
        loans = cursor.fetchall()
        for loan in loans:
            total_liabilities += float(loan['Debt'])

        # 5. Πρόσφατες Συναλλαγές
        user_account_ids = [acc['AccountID'] for acc in accounts]
        if user_account_ids:
            format_strings = ','.join(['%s'] * len(user_account_ids))
            query_trans = f"""
                SELECT t.Date, t.Time, t.Amount, at.MovementType, a.AccountNumber
                FROM transaction t 
                JOIN account_transaction at ON t.TransactionID = at.TransactionID
                JOIN account a ON at.AccountID = a.AccountID
                WHERE at.AccountID IN ({format_strings}) 
                ORDER BY t.Date DESC, t.Time DESC LIMIT 10
            """
            cursor.execute(query_trans, tuple(user_account_ids))
            recent_transactions = cursor.fetchall()
            for t in recent_transactions:
                if t['MovementType'] == 'CC_Repayment':
                    t['Amount'] = t['Amount'] / 2

        cursor.close()
        conn.close()

    net_worth = total_assets 
    
    return render_template('dashboard.html', accounts=accounts, debit_cards=debit_cards, 
                           credit_cards=credit_cards, loans=loans, transactions=recent_transactions, 
                           net_worth=net_worth, user_name=session.get('user_name', 'User'))

# ... [Οι υπόλοιπες routes (transfer, pay_loan κλπ) παραμένουν ίδιες] ...

if __name__ == '__main__':
    # Δυναμική πόρτα για το Render
    port = int(os.environ.get("PORT", 5000))
    # debug=False για την τελική έκδοση (παραγωγή)
    app.run(host='0.0.0.0', port=port, debug=False)
