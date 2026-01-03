from flask import Flask, render_template, request, redirect, url_for, session, flash
import mysql.connector
from mysql.connector import Error
import datetime
import functools
import os 

app = Flask(__name__)

# Ασφαλής ανάκτηση του Secret Key από το Render
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_12345')

def get_db_connection():
    """Σύνδεση στη βάση δεδομένων χρησιμοποιώντας Environment Variables και SSL."""
    try:
        connection = mysql.connector.connect(
            host=os.environ.get('DB_HOST'),
            user=os.environ.get('DB_USER'),
            password=os.environ.get('DB_PASSWORD'),
            database=os.environ.get('DB_NAME'),
            port=int(os.environ.get('DB_PORT', 16699)),
            ssl_disabled=False  # Απαραίτητο για το Aiven
        )
        return connection
    except Error as e:
        print(f"Connection Error: {e}")
        return None

def login_required(view):
    """Decorator για την προστασία των routes."""
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
            # Προστασία από SQL Injection με Prepared Statements
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

@app.route('/dashboard')
@login_required
def dashboard():
    """Προβολή dashboard με αρχικοποιημένες μεταβλητές για αποφυγή σφαλμάτων."""
    user_id = session['user_id']
    conn = get_db_connection()
    
    accounts, debit_cards, credit_cards, loans, recent_transactions = [], [], [], [], []
    total_assets = 0.0
    
    if conn:
        cursor = conn.cursor(dictionary=True)
        # 1. Accounts & Net Worth
        cursor.execute("SELECT ca.*, acc_table.Status, COALESCE(ab.Balance, 0) as Balance, CASE WHEN sa.AccountID IS NOT NULL THEN 'Savings' WHEN cha.AccountID IS NOT NULL THEN 'Checking' ELSE 'General' END as AccountType FROM customer_accounts ca JOIN account acc_table ON ca.AccountID = acc_table.AccountID LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s", (user_id,))
        accounts = cursor.fetchall()
        for acc in accounts: total_assets += float(acc['Balance'])

        # 2. Debit & Credit Cards
        cursor.execute("SELECT dc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV, a.AccountNumber FROM debit_card dc JOIN card c ON dc.CardID = c.CardID JOIN account a ON dc.AccountID = a.AccountID WHERE a.CustomerID = %s AND c.Status = 'Active'", (user_id,))
        debit_cards = cursor.fetchall()
        cursor.execute("SELECT cc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV, cc.CreditLimit, ccb.AvailableBalance FROM credit_card cc JOIN card c ON cc.CardID = c.CardID LEFT JOIN credit_card_balance ccb ON cc.CardID = ccb.CardID WHERE cc.CustomerID = %s AND c.Status = 'Active'", (user_id,))
        credit_cards = cursor.fetchall()

        # 3. Loans
        cursor.execute("SELECT LoanID, Type, Amount, ExpirationDate, Debt FROM loan_debts WHERE CustomerID = %s AND Debt > 0", (user_id,))
        loans = cursor.fetchall()

        # 4. Recent Transactions (με διόρθωση Amount/2 για πληρωμές καρτών)
        user_account_ids = [acc['AccountID'] for acc in accounts]
        if user_account_ids:
            format_strings = ','.join(['%s'] * len(user_account_ids))
            query_trans = f"SELECT t.*, at.MovementType, a.AccountNumber FROM transaction t JOIN account_transaction at ON t.TransactionID = at.TransactionID JOIN account a ON at.AccountID = a.AccountID WHERE at.AccountID IN ({format_strings}) ORDER BY t.Date DESC, t.Time DESC LIMIT 10"
            cursor.execute(query_trans, tuple(user_account_ids))
            recent_transactions = cursor.fetchall()
            for t in recent_transactions:
                if t['MovementType'] == 'CC_Repayment': t['Amount'] = t['Amount'] / 2

        cursor.close()
        conn.close()

    return render_template('dashboard.html', accounts=accounts, debit_cards=debit_cards, credit_cards=credit_cards, loans=loans, transactions=recent_transactions, net_worth=total_assets, user_name=session.get('user_name', 'User'))

@app.route('/transfer', methods=['GET', 'POST'])
@login_required
def transfer():
    """Μεταφορά χρημάτων με ACID Transactions."""
    if request.method == 'POST':
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            source_id = request.form['source_account_id']
            dest_number = request.form['dest_account_number']
            amount = float(request.form['amount'])
            

            cursor.execute("SELECT AccountID FROM account WHERE AccountNumber = %s", (dest_number,))
            dest_row = cursor.fetchone()
            if not dest_row: raise Exception("Destination account not found.")
            
            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            last_tid = cursor.fetchone()['COALESCE(MAX(TransactionID), 0)']
            
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (last_tid + 1, datetime.datetime.now().date(), datetime.datetime.now().time(), -amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'Transfer_OUT')", (last_tid + 1, source_id))
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (last_tid + 2, datetime.datetime.now().date(), datetime.datetime.now().time(), amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'Transfer_IN')", (last_tid + 2, dest_row['AccountID']))
            
            conn.commit()
            flash('Transfer successful!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f'Transfer failed: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s", (session['user_id'],))
    accounts = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('transfer.html', accounts=accounts)

@app.route('/pay_loan', methods=['GET', 'POST'])
@login_required
def pay_loan():
    """Πληρωμή δανείου με καθαρή διαχείριση σύνδεσης."""
    if request.method == 'POST':
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            source_id = request.form['source_account_id']
            amount = float(request.form['amount'])

            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            next_tid = cursor.fetchone()['COALESCE(MAX(TransactionID), 0)'] + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (next_tid, datetime.datetime.now().date(), datetime.datetime.now().time(), -amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'LoanPayment')", (next_tid, source_id))
            conn.commit()
            flash('Loan payment successful!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f'Payment failed: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM loan_debts WHERE CustomerID = %s AND Debt > 0", (session['user_id'],))
    loan = cursor.fetchone()
    cursor.execute("SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s", (session['user_id'],))
    accounts = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('pay_loan.html', loan=loan, accounts=accounts)

@app.route('/pay_credit', methods=['GET', 'POST'])
@login_required
def pay_credit():
    """Πληρωμή κάρτας με Double Transaction Strategy."""
    if request.method == 'POST':
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            source_id, card_id, amount = request.form['source_account_id'], request.form['card_id'], float(request.form['amount'])

            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            last_tid = cursor.fetchone()['COALESCE(MAX(TransactionID), 0)']
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (last_tid + 1, datetime.datetime.now().date(), datetime.datetime.now().time(), amount))
            cursor.execute("INSERT INTO credit_payment_transaction (TransactionID, AccountID, CardID) VALUES (%s, %s, %s)", (last_tid + 1, source_id, card_id))
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (last_tid + 2, datetime.datetime.now().date(), datetime.datetime.now().time(), -2 * amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'CC_Repayment')", (last_tid + 2, source_id))
            conn.commit()
            flash('Credit card payment successful!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f'Payment failed: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s", (session['user_id'],))
    accounts = cursor.fetchall()
    cursor.execute("SELECT cc.CardID, c.CardNumber, c.CardholderName, cc.CreditLimit FROM credit_card cc JOIN card c ON cc.CardID = c.CardID LEFT JOIN credit_card_balance ccb ON cc.CardID = ccb.CardID WHERE cc.CustomerID = %s AND c.Status = 'Active'", (session['user_id'],))
    credit_cards = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('pay_credit.html', accounts=accounts, credit_cards=credit_cards)

@app.route('/branches')
def branches():
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT b.*, GROUP_CONCAT(DISTINCT t.Tel SEPARATOR ', ') as Phones, GROUP_CONCAT(DISTINCT e.Email SEPARATOR ', ') as Emails FROM bank_branch b LEFT JOIN bank_branch_tel t ON b.BranchID = t.BranchID LEFT JOIN bank_branch_email e ON b.BranchID = e.BranchID GROUP BY b.BranchID")
        branches = cursor.fetchall()
        for b in branches:
            b['Phones'] = b['Phones'].split(', ') if b['Phones'] else []
            b['Emails'] = b['Emails'].split(', ') if b['Emails'] else []
        cursor.close()
        conn.close()
        return render_template('branches.html', branches=branches)
    return redirect(url_for('home'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_id = session['user_id']
    if request.method == 'POST':
        conn = get_db_connection()
        cursor = conn.cursor()
        action = request.form.get('action')
        try:
            if action == 'update_address': cursor.execute("UPDATE customer SET Address = %s WHERE CustomerID = %s", (request.form['address'], user_id))
            elif action == 'add_email': cursor.execute("INSERT INTO customer_email (CustomerID, Email) VALUES (%s, %s)", (user_id, request.form['new_email']))
            elif action == 'delete_email': cursor.execute("DELETE FROM customer_email WHERE CustomerID = %s AND Email = %s", (user_id, request.form['email_to_delete']))
            conn.commit()
            flash('Settings updated!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('settings'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT DISTINCT Name, TIN FROM customer_accounts WHERE CustomerID = %s", (user_id,))
    identity = cursor.fetchone()
    cursor.execute("SELECT Address FROM customer WHERE CustomerID = %s", (user_id,))
    address = cursor.fetchone()['Address']
    cursor.execute("SELECT Tel FROM customer_tel WHERE CustomerID = %s", (user_id,))
    phones = cursor.fetchall()
    cursor.execute("SELECT Email FROM customer_email WHERE CustomerID = %s", (user_id,))
    emails = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('settings.html', identity=identity, address=address, phones=phones, emails=emails)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
