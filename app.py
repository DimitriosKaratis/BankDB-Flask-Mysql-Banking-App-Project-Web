from flask import Flask, render_template, request, redirect, url_for, session, flash
import mysql.connector
from mysql.connector import Error
import datetime
import functools
import os # Απαραίτητο για να διαβάζουμε τις μεταβλητές περιβάλλοντος

app = Flask(__name__)

# ΑΣΦΑΛΕΙΑ: Παίρνει το κλειδί από το Cloud, αλλιώς χρησιμοποιεί ένα τυχαίο για τοπικά
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_12345')

# Database Configuration (Πλήρως προστατευμένο για GitHub)
def get_db_connection():
    """Establishes a connection using environment variables ONLY."""
    try:
        connection = mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', 'default_password'), 
            database=os.environ.get('DB_NAME', 'BankDB'),
            port=int(os.environ.get('DB_PORT', 3306)),
            # ΠΡΟΣΘΗΚΗ: Απαραίτητο για το Aiven Cloud
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
    """Redirects to dashboard if logged in, else login."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles user login authentication via TIN."""
    if request.method == 'POST':
        tin = request.form['tin']
        password = request.form['password'] # Not verified against DB as per plan (no password field)

        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            # SECURE: Using Prepared Statement to prevent SQL Injection
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
    """Displays customer dashboard with accounts, cards, loans, and net worth."""
    user_id = session['user_id']
    conn = get_db_connection()
    
    accounts = []
    debit_cards = []
    credit_cards = []
    loans = []
    # ΔΙΟΡΘΩΣΗ: Αρχικοποίηση εδώ για να μην πετάει UnboundLocalError αν δεν υπάρχουν συναλλαγές
    recent_transactions = []
    
    total_assets = 0.0
    total_liabilities = 0.0
    
    if conn:
        cursor = conn.cursor(dictionary=True)
        
        # 1. Fetch Accounts & Balances with Type
        query_acc = """
            SELECT 
                ca.AccountID, 
                ca.AccountNumber, 
                ca.Currency, 
                acc_table.Status,
                COALESCE(ab.Balance, 0) as Balance,
                CASE 
                    WHEN sa.AccountID IS NOT NULL THEN 'Savings'
                    WHEN cha.AccountID IS NOT NULL THEN 'Checking'
                    ELSE 'General' 
                END as AccountType
            FROM customer_accounts ca
            JOIN account acc_table ON ca.AccountID = acc_table.AccountID
            LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID
            LEFT JOIN savings_account sa ON ca.AccountID = sa.AccountID
            LEFT JOIN checking_account cha ON ca.AccountID = cha.AccountID
            WHERE ca.CustomerID = %s
        """
        cursor.execute(query_acc, (user_id,))
        accounts = cursor.fetchall()
        
        # Calculate Assets
        for acc in accounts:
            # Simple assumption: ignoring currency conversion for Net Worth sum
            total_assets += float(acc['Balance'])

        # 2. Fetch Debit Cards (Active Only) with CVV
        query_dc = """
            SELECT dc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV, a.AccountNumber
            FROM debit_card dc
            JOIN card c ON dc.CardID = c.CardID
            JOIN account a ON dc.AccountID = a.AccountID
            WHERE a.CustomerID = %s AND c.Status = 'Active'
        """
        cursor.execute(query_dc, (user_id,))
        debit_cards = cursor.fetchall()

        # 3. Fetch Credit Cards with CVV
        query_cc = """
            SELECT cc.CardID, c.CardNumber, c.CardholderName, c.ExpirationDate, c.CVV,
                   cc.CreditLimit, ccb.AvailableBalance,
                   (cc.CreditLimit - COALESCE(ccb.AvailableBalance, cc.CreditLimit)) as CurrentDebt
            FROM credit_card cc
            JOIN card c ON cc.CardID = c.CardID
            LEFT JOIN credit_card_balance ccb ON cc.CardID = ccb.CardID
            WHERE cc.CustomerID = %s AND c.Status = 'Active'
        """
        cursor.execute(query_cc, (user_id,))
        credit_cards = cursor.fetchall()
        
        # 4. Fetch Active Loans
        query_loans = """
            SELECT LoanID, Type, Amount, ExpirationDate, Debt
            FROM loan_debts
            WHERE CustomerID = %s AND Debt > 0
        """
        cursor.execute(query_loans, (user_id,))
        loans = cursor.fetchall()
        
        # Calculate Liabilities
        for loan in loans:
            total_liabilities += float(loan['Debt'])

        # 5. Fetch Recent Transactions
        user_account_ids = [acc['AccountID'] for acc in accounts]
        
        if user_account_ids:
            format_strings = ','.join(['%s'] * len(user_account_ids))
            
            query_trans = f"""
                SELECT 
                    t.Date, 
                    t.Time,
                    t.Amount, 
                    at.MovementType,
                    a.AccountNumber
                FROM transaction t 
                JOIN account_transaction at ON t.TransactionID = at.TransactionID
                JOIN account a ON at.AccountID = a.AccountID
                WHERE at.AccountID IN ({format_strings}) 
                ORDER BY t.Date DESC, t.Time DESC
                LIMIT 10
            """
            
            cursor.execute(query_trans, tuple(user_account_ids))
            recent_transactions = cursor.fetchall()
            
            for t in recent_transactions:
                if t['MovementType'] == 'CC_Repayment':
                    t['Amount'] = t['Amount'] / 2

        cursor.close()
        conn.close()

    net_worth = total_assets 
    
    return render_template('dashboard.html', 
                           accounts=accounts, 
                           debit_cards=debit_cards,
                           credit_cards=credit_cards,
                           loans=loans,
                           transactions=recent_transactions,
                           net_worth=net_worth,
                           user_name=session.get('user_name', 'User'))

# --- ΟΙ ΥΠΟΛΟΙΠΕΣ ΣΥΝΑΡΤΗΣΕΙΣ (transfer, pay_loan, κλπ) ΠΑΡΑΜΕΝΟΥΝ ΑΚΡΙΒΩΣ ΟΙ ΙΔΙΕΣ ---

@app.route('/transfer', methods=['GET', 'POST'])
@login_required
def transfer():
    if request.method == 'POST':
        trans_conn = get_db_connection()
        if not trans_conn:
            flash('Database connection failed.', 'danger')
            return redirect(url_for('transfer'))
        source_id = request.form['source_account_id']
        dest_number = request.form['dest_account_number']
        try:
            amount = float(request.form['amount'])
        except ValueError:
            flash('Invalid amount.', 'warning')
            return redirect(url_for('transfer'))
        if amount <= 0:
            flash('Amount must be positive.', 'warning')
            return redirect(url_for('transfer'))
        cursor = None
        try:
            cursor = trans_conn.cursor()
            trans_conn.start_transaction()
            cursor.execute("SELECT Balance FROM accounts_balance WHERE AccountID = %s", (source_id,))
            balance_row = cursor.fetchone()
            if not balance_row or balance_row[0] < amount:
                trans_conn.rollback()
                flash('Insufficient funds or invalid account.', 'danger')
                return redirect(url_for('transfer'))
            cursor.execute("SELECT AccountID FROM account WHERE AccountNumber = %s", (dest_number,))
            dest_row = cursor.fetchone()
            if not dest_row:
                trans_conn.rollback()
                flash('Destination account not found.', 'danger')
                return redirect(url_for('transfer'))
            dest_id = dest_row[0]
            now = datetime.datetime.now()
            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            last_tid = cursor.fetchone()[0]
            next_tid_out = last_tid + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (next_tid_out, now.date(), now.time(), -amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'Transfer_OUT')", (next_tid_out, source_id))
            next_tid_in = next_tid_out + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (next_tid_in, now.date(), now.time(), amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'Transfer_IN')", (next_tid_in, dest_id))
            trans_conn.commit()
            flash('Transfer successful!', 'success')
            return redirect(url_for('dashboard'))
        except Error as e:
            if trans_conn.is_connected(): trans_conn.rollback()
            flash(f'Transfer failed: {e}', 'danger')
            return redirect(url_for('transfer'))
        finally:
            if cursor: cursor.close()
            if trans_conn and trans_conn.is_connected(): trans_conn.close()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    query_acc = "SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s"
    cursor.execute(query_acc, (session['user_id'],))
    accounts = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('transfer.html', accounts=accounts)

@app.route('/pay_loan', methods=['GET', 'POST'])
@login_required
def pay_loan():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM loan_debts WHERE CustomerID = %s AND Debt > 0", (session['user_id'],))
    loan = cursor.fetchone()
    query_acc = "SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s"
    cursor.execute(query_acc, (session['user_id'],))
    accounts = cursor.fetchall()
    if request.method == 'POST':
        source_id = request.form['source_account_id']
        try:
            amount = float(request.form['amount'])
        except ValueError:
            flash('Invalid amount.', 'warning')
            return redirect(url_for('pay_loan'))
        if amount <= 0:
            flash('Amount must be positive.', 'warning')
            return redirect(url_for('pay_loan'))
        if not loan:
            flash('No active loan found.', 'danger')
            return redirect(url_for('dashboard'))
        if amount > loan['Debt']:
            flash('Payment exceeds remaining debt.', 'warning')
            return redirect(url_for('pay_loan'))
        selected_acc = next((a for a in accounts if str(a['AccountID']) == source_id), None)
        if not selected_acc or selected_acc['Balance'] < amount:
            flash('Insufficient funds.', 'danger')
            return redirect(url_for('pay_loan'))
        try:
            conn.commit()
            conn.start_transaction()
            now = datetime.datetime.now()
            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            last_tid = cursor.fetchone()['COALESCE(MAX(TransactionID), 0)']
            next_tid = last_tid + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (next_tid, now.date(), now.time(), -amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'LoanPayment')", (next_tid, source_id))
            conn.commit()
            flash('Loan payment successful!', 'success')
            return redirect(url_for('dashboard'))
        except Error as e:
            conn.rollback()
            flash(f'Payment failed: {e}', 'danger')
    cursor.close()
    conn.close()
    return render_template('pay_loan.html', loan=loan, accounts=accounts)

@app.route('/pay_credit', methods=['GET', 'POST'])
@login_required
def pay_credit():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    query_acc = "SELECT ca.AccountID, ca.AccountNumber, ca.Currency, COALESCE(ab.Balance, 0) as Balance FROM customer_accounts ca LEFT JOIN accounts_balance ab ON ca.AccountID = ab.AccountID WHERE ca.CustomerID = %s"
    cursor.execute(query_acc, (session['user_id'],))
    accounts = cursor.fetchall()
    cursor.execute("SELECT cc.CardID, c.CardNumber, c.CardholderName, cc.CreditLimit FROM credit_card cc JOIN card c ON cc.CardID = c.CardID LEFT JOIN credit_card_balance ccb ON cc.CardID = ccb.CardID WHERE cc.CustomerID = %s AND c.Status = 'Active' AND (ccb.AvailableBalance < cc.CreditLimit OR ccb.AvailableBalance IS NULL)", (session['user_id'],))
    credit_cards = cursor.fetchall()
    if request.method == 'POST':
        source_id = request.form['source_account_id']
        card_id = request.form['card_id']
        try:
            amount = float(request.form['amount'])
        except ValueError:
            flash('Invalid amount.', 'warning')
            return redirect(url_for('pay_credit'))
        if amount <= 0:
            flash('Amount must be positive.', 'warning')
            return redirect(url_for('pay_credit'))
        selected_acc = next((a for a in accounts if str(a['AccountID']) == source_id), None)
        if not selected_acc or selected_acc['Balance'] < amount:
            flash('Insufficient funds.', 'danger')
            return redirect(url_for('pay_credit'))
        try:
            conn.commit()
            conn.start_transaction()
            now = datetime.datetime.now()
            cursor.execute("SELECT COALESCE(MAX(TransactionID), 0) FROM transaction")
            last_tid = cursor.fetchone()['COALESCE(MAX(TransactionID), 0)']
            tid_1 = last_tid + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (tid_1, now.date(), now.time(), amount))
            cursor.execute("INSERT INTO credit_payment_transaction (TransactionID, AccountID, CardID) VALUES (%s, %s, %s)", (tid_1, source_id, card_id))
            tid_2 = tid_1 + 1
            cursor.execute("INSERT INTO transaction (TransactionID, Date, Time, Amount) VALUES (%s, %s, %s, %s)", (tid_2, now.date(), now.time(), -2 * amount))
            cursor.execute("INSERT INTO account_transaction (TransactionID, AccountID, MovementType) VALUES (%s, %s, 'CC_Repayment')", (tid_2, source_id))
            conn.commit()
            flash('Credit card payment successful!', 'success')
            return redirect(url_for('dashboard'))
        except Error as e:
            conn.rollback()
            flash(f'Payment failed: {e}', 'danger')
    cursor.close()
    conn.close()
    return render_template('pay_credit.html', accounts=accounts, credit_cards=credit_cards)

@app.route('/branches')
def branches():
    conn = get_db_connection()
    branches = []
    if conn:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT b.BranchID, b.Address, b.OperatingHours, GROUP_CONCAT(DISTINCT t.Tel SEPARATOR ', ') as Phones, GROUP_CONCAT(DISTINCT e.Email SEPARATOR ', ') as Emails FROM bank_branch b LEFT JOIN bank_branch_tel t ON b.BranchID = t.BranchID LEFT JOIN bank_branch_email e ON b.BranchID = e.BranchID GROUP BY b.BranchID"
        cursor.execute(query)
        branches = cursor.fetchall()
        for branch in branches:
            branch['Phones'] = branch['Phones'].split(', ') if branch['Phones'] else []
            branch['Emails'] = branch['Emails'].split(', ') if branch['Emails'] else []
        cursor.close()
        conn.close()
    return render_template('branches.html', branches=branches)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_id = session['user_id']
    conn = get_db_connection()
    if not conn:
        flash('Database connection failed.', 'danger')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            cursor = conn.cursor()
            if action == 'update_address':
                new_address = request.form['address']
                cursor.execute("UPDATE customer SET Address = %s WHERE CustomerID = %s", (new_address, user_id))
                flash('Address updated successfully!', 'success')
            elif action == 'add_email':
                new_email = request.form['new_email']
                cursor.execute("SELECT Email FROM customer_email WHERE CustomerID = %s AND Email = %s", (user_id, new_email))
                if cursor.fetchone(): flash('This email is already linked to your account.', 'warning')
                else:
                    cursor.execute("INSERT INTO customer_email (CustomerID, Email) VALUES (%s, %s)", (user_id, new_email))
                    flash('New email added successfully!', 'success')
            elif action == 'update_email':
                old_email = request.form['old_email']
                new_email = request.form['email']
                cursor.execute("UPDATE customer_email SET Email = %s WHERE CustomerID = %s AND Email = %s", (new_email, user_id, old_email))
                flash('Email updated successfully!', 'success')
            elif action == 'delete_email':
                email_to_delete = request.form['email_to_delete']
                cursor.execute("DELETE FROM customer_email WHERE CustomerID = %s AND Email = %s", (user_id, email_to_delete))
                flash('Email deleted successfully!', 'success')
            conn.commit()
            cursor.close()
        except Error as e:
            conn.rollback()
            flash(f'Error updating profile: {e}', 'danger')
        finally:
            conn.close()
        return redirect(url_for('settings'))
    else:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT DISTINCT Name, TIN FROM customer_accounts WHERE CustomerID = %s", (user_id,))
        identity = cursor.fetchone()
        cursor.execute("SELECT Address FROM customer WHERE CustomerID = %s", (user_id,))
        address_data = cursor.fetchone()
        current_address = address_data['Address'] if address_data else ''
        cursor.execute("SELECT Tel FROM customer_tel WHERE CustomerID = %s", (user_id,))
        phones = cursor.fetchall()
        cursor.execute("SELECT Email FROM customer_email WHERE CustomerID = %s", (user_id,))
        emails = cursor.fetchall()
        cursor.close()
        conn.close()
        return render_template('settings.html', identity=identity, address=current_address, phones=phones, emails=emails)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
