# app.py
import os
import json
import time
import csv
import threading
import pytz
import re
import os
import psycopg2
from datetime import datetime
from psycopg2 import Error
from flask import Flask, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)

# Database Configuration
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    # Parse Render's DATABASE_URL format
    url_match = re.match(r'postgres://(.*?):(.*?)@(.*?):(.*?)/(.*)', DATABASE_URL)
    if url_match:
        DB_CONFIG = {
            'host': url_match.group(3),
            'user': url_match.group(1),
            'password': url_match.group(2),
            'database': url_match.group(5),
            'port': url_match.group(4)
        }
    else:
        raise ValueError("Could not parse DATABASE_URL")
else:  # Fallback for local development
    DB_CONFIG = {
        'host': os.environ.get('DB_HOST'),
        'user': os.environ.get('DB_USER'),
        'password': os.environ.get('DB_PASSWORD'),
        'database': os.environ.get('DB_NAME'),
        'port': os.environ.get('DB_PORT', '5432')
    }

# Use environment variables for secrets
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key')  # <-- Replace hardcoded key
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'password123')

# Other Configuration
OUTPUT_JSON = 'feed1.json'
POLL_INTERVAL = 60  # seconds
EASTERN_TZ = pytz.timezone('America/New_York')
UTC = pytz.utc
CONTACT_NAME = "Pruthvi Yannam"
CONTACT_EMAIL = "pruthvi.yannam@example.com"

# Global state
processed_ids = set()

def get_db_connection():
    """Create and return a new database connection"""
    try:
        return psycopg2.connect(**DB_CONFIG)
    except Error as e:
        print(f"Database connection error: {e}")
        return None

def is_valid_event(row):
    """Check if event should be included based on status and time"""
    if row['lane_status'].lower() in ['inactive', 'complete', 'completed']:
        return False
    
    try:
        end_dt = parse_datetime(row['end_time'])
        if end_dt < datetime.now(UTC):
            return False
    except Exception:
        pass
    
    return True

def convert_to_local_time(time_input):
    """Convert UTC time to Virginia local time"""
    if isinstance(time_input, str):
        try:
            # Parse string first
            dt = parse_datetime(time_input)
        except ValueError:
            return time_input
    elif isinstance(time_input, datetime):
        dt = time_input
    else:
        return str(time_input)
    
    try:
        return dt.astimezone(EASTERN_TZ).strftime('%b %d, %Y %I:%M %p %Z')
    except Exception:
        return str(time_input)

def parse_datetime(timestamp):
    """Robust datetime parsing with ISO 8601, Zulu time, and datetime object support"""
    if isinstance(timestamp, datetime):
        # If already a datetime object, ensure it's timezone-aware
        if timestamp.tzinfo is None:
            return EASTERN_TZ.localize(timestamp).astimezone(UTC)
        return timestamp.astimezone(UTC)
    
    if not isinstance(timestamp, str):
        raise ValueError(f"Invalid timestamp type: {type(timestamp)}")
    
    if not timestamp:
        raise ValueError("Empty timestamp string")
    
    # Handle ISO 8601 format with 'Z' (Zulu/UTC time)
    if 'T' in timestamp and timestamp.endswith('Z'):
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return dt.astimezone(UTC)
        except ValueError:
            pass
    
    # Handle other formats
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",  # ISO with timezone
        "%Y-%m-%d %H:%M:%S%z",   # Standard with timezone
        "%Y-%m-%dT%H:%M:%S",     # ISO without timezone
        "%Y-%m-%d %H:%M:%S",     # Standard without timezone
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M"
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(timestamp, fmt)
            # Localize if no timezone info
            if dt.tzinfo is None:
                dt = EASTERN_TZ.localize(dt)
            return dt.astimezone(UTC)
        except ValueError:
            continue
    
    # print(f"Failed to parse timestamp: '{timestamp}'")
    raise ValueError(f"Unrecognized time format: '{timestamp}'")

def fetch_all_events():
    """Fetch all events from database with error handling"""
    conn = get_db_connection()
    if not conn:
        print("Failed to connect to database")
        return []
    
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM lane_events ORDER BY start_time DESC"
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        events = [dict(zip(columns, row)) for row in cursor.fetchall()]
        # print(f"Successfully fetched {len(events)} events from database")
        return events
    except Error as e:
        print(f"Database query error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def fetch_event_by_id(event_id):
    """Fetch a specific event by ID"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM lane_events WHERE id = %s"
        cursor.execute(query, (event_id,))
        columns = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        return dict(zip(columns, row)) if row else None
    except Error as e:
        print(f"Database query error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def fetch_latest_events():
    """Fetch latest events from database using MAX(id) per event_id"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        query = """
            SELECT t1.*
            FROM lane_events t1
            JOIN (
                SELECT event_id, MAX(id) AS max_id
                FROM lane_events
                GROUP BY event_id
            ) t2 ON t1.id = t2.max_id
        """
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        events = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return events
    except Error as e:
        print(f"Database query error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def categorize_events(events):
    """Categorize events with enhanced error handling"""
    active = []
    completed = []
    upcoming = []
    
    now = datetime.now(UTC)
    # print(f"Current UTC time: {now}")
    
    for event in events:
        try:
            # Skip if critical fields are missing
            if not all(key in event for key in ['start_time', 'end_time', 'lane_status']):
                # print(f"Skipping event {event.get('id', 'unknown')}: Missing required fields")
                continue
                
            start_dt = parse_datetime(event['start_time'])
            end_dt = parse_datetime(event['end_time'])
            status = event['lane_status'].lower()
            
            # Check if event is completed
            if status in ['complete', 'completed'] or end_dt < now:
                completed.append(event)
            # Check if event is upcoming
            elif start_dt > now:
                upcoming.append(event)
            # Otherwise active
            else:
                active.append(event)
                
        except Exception as e:
            # print(f"Error processing event {event.get('id', 'unknown')}: {str(e)}")
            # Fallback to active status if we can't parse times
            active.append(event)
    
    # print(f"Categorized: {len(active)} active, {len(upcoming)} upcoming, {len(completed)} completed")
    return active, completed, upcoming

def update_event(event_id, data):
    """Update an existing event in the database"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        query = """
            UPDATE lane_events 
            SET 
                event_id = %s,
                district = %s,
                start_time = %s,
                end_time = %s,
                lat_start = %s,
                lon_start = %s,
                lat_end = %s,
                lon_end = %s,
                lane_status = %s,
                road_name = %s
            WHERE id = %s
        """
        values = (
            data['event_id'],
            data['district'],
            data['start_time'],
            data['end_time'],
            data['lat_start'],
            data['lon_start'],
            data['lat_end'],
            data['lon_end'],
            data['lane_status'],
            data['road_name'],
            event_id
        )
        cursor.execute(query, values)
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Update error: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def create_event(data):
    """Create a new event in the database with proper data type handling"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        # Convert coordinates to float
        lat_start = float(data['lat_start'])
        lon_start = float(data['lon_start'])
        lat_end = float(data['lat_end'])
        lon_end = float(data['lon_end'])
        
        cursor = conn.cursor()
        query = """
            INSERT INTO lane_events 
            (event_id, district, start_time, end_time, 
             lat_start, lon_start, lat_end, lon_end, 
             lane_status, road_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        values = (
            data['event_id'],
            data['district'],
            data['start_time'],
            data['end_time'],
            lat_start,
            lon_start,
            lat_end,
            lon_end,
            data['lane_status'],
            data['road_name']
        )
        cursor.execute(query, values)
        new_id = cursor.fetchone()[0]
        conn.commit()
        return new_id
    except ValueError as e:
        print(f"Invalid coordinate value: {e}")
        return None
    except Error as e:
        print(f"Create error: {e}")
        conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def delete_event(event_id):
    """Delete an event from the database"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        query = "DELETE FROM lane_events WHERE id = %s"
        cursor.execute(query, (event_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Delete error: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def upload_csv_to_db(csv_path):
    """Upload CSV data to PostgreSQL database"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                query = """
                    INSERT INTO lane_events 
                    (event_id, district, start_time, end_time, 
                     lat_start, lon_start, lat_end, lon_end, 
                     lane_status, road_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                values = (
                    row['event_id'], 
                    row['district'], 
                    row['start_time'], 
                    row['end_time'],
                    float(row['lat_start']), 
                    float(row['lon_start']),
                    float(row['lat_end']), 
                    float(row['lon_end']),
                    row['lane_status'], 
                    row['road_name']
                )
                cursor.execute(query, values)
        conn.commit()
        return True
    except Error as e:
        print(f"Upload error: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

# Flask Routes
@app.route('/')
def home():
    if 'username' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    events = fetch_latest_events()  # Use latest events for categorization
    active, completed, upcoming = categorize_events(events)
    
    # Convert times to Eastern Time for display
    for event_list in [active, upcoming]:
        for event in event_list:
            event['start_time_edt'] = convert_to_local_time(event['start_time'])
            event['end_time_edt'] = convert_to_local_time(event['end_time'])
    
    # Get last updated time
    now = datetime.now(EASTERN_TZ)
    last_updated = now.strftime('%b %d, %Y %I:%M %p %Z')
    
    return render_template('dashboard.html', 
                           active_count=len(active),
                           completed_count=len(completed),
                           upcoming_count=len(upcoming),
                           last_updated=last_updated,
                           active_events=active[:5],
                           upcoming_events=upcoming[:5])

@app.route('/events')
def events():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    # Get only the latest events (one per event_id)
    latest_events = fetch_latest_events()
    active, completed, upcoming = categorize_events(latest_events)
    
    return render_template('events.html', 
                           active_events=active,
                           completed_events=completed,
                           upcoming_events=upcoming,
                           all_events=latest_events)

@app.route('/event/<int:event_id>')
def event_detail(event_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    
    event = fetch_event_by_id(event_id)
    if not event:
        flash('Event not found', 'error')
        return redirect(url_for('events'))
    
    # Convert times to Eastern Time for display
    try:
        start_dt = parse_datetime(event['start_time']).astimezone(EASTERN_TZ)
        end_dt = parse_datetime(event['end_time']).astimezone(EASTERN_TZ)
        event['start_time_display'] = start_dt.strftime('%b %d, %Y %I:%M %p %Z')
        event['end_time_display'] = end_dt.strftime('%b %d, %Y %I:%M %p %Z')
    except Exception as e:
        print(f"Error parsing times: {e}")
        event['start_time_display'] = event['start_time']
        event['end_time_display'] = event['end_time']
    
    # Determine status for display
    now = datetime.now(UTC)
    try:
        end_dt = parse_datetime(event['end_time'])
        if event['lane_status'].lower() in ['complete', 'completed'] or end_dt < now:
            event['status'] = 'completed'
        else:
            start_dt = parse_datetime(event['start_time'])
            if start_dt > now:
                event['status'] = 'upcoming'
            else:
                event['status'] = 'active'
    except:
        event['status'] = 'active'
    
    return render_template('event_detail.html', event=event)

@app.route('/event/edit/<int:event_id>', methods=['GET', 'POST'])
def edit_event(event_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    
    event = fetch_event_by_id(event_id)
    if not event:
        flash('Event not found', 'error')
        return redirect(url_for('events'))
    
    if request.method == 'POST':
        # Get form data
        form_data = {
            'event_id': request.form['event_id'],
            'district': request.form['district'],
            'start_time': request.form['start_time'],
            'end_time': request.form['end_time'],
            'lat_start': request.form['lat_start'],
            'lon_start': request.form['lon_start'],
            'lat_end': request.form['lat_end'],
            'lon_end': request.form['lon_end'],
            'lane_status': request.form['lane_status'],
            'road_name': request.form['road_name']
        }
        
        # Update event
        if update_event(event_id, form_data):
            flash('Event updated successfully!', 'success')
            return redirect(url_for('event_detail', event_id=event_id))
        else:
            flash('Failed to update event', 'error')
    
    # Convert times to datetime-local format for form
    try:
        start_dt = parse_datetime(event['start_time']).astimezone(EASTERN_TZ)
        end_dt = parse_datetime(event['end_time']).astimezone(EASTERN_TZ)
        event['start_time_local'] = start_dt.strftime('%Y-%m-%dT%H:%M')
        event['end_time_local'] = end_dt.strftime('%Y-%m-%dT%H:%M')
    except Exception as e:
        print(f"Error parsing times: {e}")
        event['start_time_local'] = event['start_time']
        event['end_time_local'] = event['end_time']
    
    return render_template('edit_event.html', event=event)

@app.route('/event/delete/<int:event_id>', methods=['POST'])
def delete_event_route(event_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    
    if delete_event(event_id):
        flash('Event deleted successfully!', 'success')
    else:
        flash('Failed to delete event', 'error')
    
    return redirect(url_for('events'))

@app.route('/event/new', methods=['GET', 'POST'])
def new_event():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        # Get form data
        form_data = {
            'event_id': request.form['event_id'],
            'district': request.form['district'],
            'start_time': request.form['start_time'],
            'end_time': request.form['end_time'],
            'lat_start': request.form['lat_start'],
            'lon_start': request.form['lon_start'],
            'lat_end': request.form['lat_end'],
            'lon_end': request.form['lon_end'],
            'lane_status': request.form['lane_status'],
            'road_name': request.form['road_name']
        }
        
        # Debug logging
        # print("Form data received:")
        # print(json.dumps(form_data, indent=2))
        
        # Create event
        new_id = create_event(form_data)
        if new_id:
            flash('Event created successfully!', 'success')
            return redirect(url_for('event_detail', event_id=new_id))
        else:
            flash('Failed to create event', 'error')
    
    return render_template('add_event.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_csv():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        if 'csv_file' not in request.files:
            flash('No file part', 'error')
            return redirect(request.url)
        
        file = request.files['csv_file']
        if file.filename == '':
            flash('No selected file', 'error')
            return redirect(request.url)
        
        if file and file.filename.endswith('.csv'):
            # Save the file
            uploads_dir = os.path.join(os.getcwd(), 'Uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            file_path = os.path.join(uploads_dir, file.filename)
            file.save(file_path)
            
            # Upload to database
            if upload_csv_to_db(file_path):
                flash('CSV file uploaded successfully! Database updated.', 'success')
            else:
                flash('Failed to upload CSV to database', 'error')
            
            # Remove the file after processing
            os.remove(file_path)
            
            return redirect(url_for('events'))
        else:
            flash('Invalid file type. Only CSV files are allowed.', 'error')
    
    return render_template('upload.html')

# Background polling thread
def background_poller():
    while True:
        # print(f"Checking database at {datetime.now(EASTERN_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}...")
        process_db_update()
        time.sleep(POLL_INTERVAL)

def process_db_update():
    """Process database updates and generate JSON"""
    global processed_ids
    
    try:
        records = fetch_latest_events()
        valid_features = []
        
        for row in records:
            if is_valid_event(row):
                feature = convert_to_wzdx(row)
                if feature:
                    valid_features.append(feature)
        
        feed = generate_feed(valid_features)
        
        current_ids = {f['id'] for f in valid_features}
        new_ids = current_ids - processed_ids
        removed_ids = processed_ids - current_ids
        
        # if new_ids:
        #     print(f"Added IDs: {', '.join(sorted(new_ids))}")
        # if removed_ids:
        #     print(f"Removed IDs: {', '.join(sorted(removed_ids))}")
        
        # with open(OUTPUT_JSON, 'w') as f:
        #     json.dump(feed, f, indent=2)
        
        # print(f"Generated {len(valid_features)} features in {OUTPUT_JSON}")
        processed_ids = current_ids
        
    except Exception as e:
        print(f"Processing error: {str(e)}")

def convert_to_wzdx(row):
    """Convert database row to WZDx feature"""
    event_id = row['event_id']
    
    required_fields = ['event_id', 'lat_start', 'lon_start', 
                      'lat_end', 'lon_end', 'road_name', 
                      'start_time', 'end_time']
    for field in required_fields:
        if not row.get(field):
            # print(f"Warning: Missing '{field}' in record {event_id}")
            return None
    
    lane_status = row['lane_status'].lower()
    status_map = {
        'partial': 'laneClosure',
        'full': 'roadClosure',
        'shoulder': 'shoulderClosure'
    }
    vehicle_impact_map = {
        'partial': 'some-lanes-closed',
        'full': 'all-lanes-closed',
        'shoulder': 'all-lanes-open'
    }
    
    event_type = status_map.get(lane_status, 'unknown')
    vehicle_impact = vehicle_impact_map.get(lane_status, 'unknown')
    
    try:
        coords = [
            [float(row['lon_start']), float(row['lat_start'])],
            [float(row['lon_end']), float(row['lat_end'])]
        ]
    except ValueError:
        # print(f"Invalid coordinates for event {event_id}")
        return None
    
    try:
        start_dt = parse_datetime(row['start_time'])
        end_dt = parse_datetime(row['end_time'])
        start_utc = start_dt.astimezone(UTC)
        end_utc = end_dt.astimezone(UTC)
        
        if end_utc < datetime.now(UTC):
            # print(f"Skipping expired event {event_id} (ended at {end_utc})")
            return None
            
    except Exception as e:
        # print(f"Time conversion error for {event_id}: {str(e)}")
        return None
    
    return {
        "id": event_id,
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "event_type": event_type,
            "data_source_id": "vdot-vflite",
            "road_name": row['road_name'],
            "start_date": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "vehicle_impact": vehicle_impact,
            "event_status": "active"
        }
    }

def generate_feed(features):
    """Generate WZDx JSON feed"""
    return {
        "road_event_feed_info": {
            "publisher": "Virginia Department of Transportation (demo)",
            "version": "4.2",
            "update_date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "contact_name": CONTACT_NAME,
            "contact_email": CONTACT_EMAIL
        },
        "type": "FeatureCollection",
        "features": features
    }

def initialize_database():
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lane_events (
                id SERIAL PRIMARY KEY,
                event_id TEXT NOT NULL,
                district TEXT,
                start_time TIMESTAMP WITH TIME ZONE,
                end_time TIMESTAMP WITH TIME ZONE,
                lat_start FLOAT,
                lon_start FLOAT,
                lat_end FLOAT,
                lon_end FLOAT,
                lane_status TEXT,
                road_name TEXT
            );
        """)
        conn.commit()
    except Error as e:
        print(f"Database initialization error: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    initialize_database()
    os.makedirs('Uploads', exist_ok=True)
    # Only start thread in production or when not in debug mode
    if not app.debug:
        poll_thread = threading.Thread(target=background_poller, daemon=True)
        poll_thread.start()
    app.run(debug=False)
else:
    # For Gunicorn in production
    initialize_database()
    poll_thread = threading.Thread(target=background_poller, daemon=True)
    poll_thread.start()
