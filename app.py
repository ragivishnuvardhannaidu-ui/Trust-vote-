import os
import sys
import threading
import requests
import hashlib

# Ensure backend module can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_from_directory

PORT = 5000
BOOTH_ID = 1

if len(sys.argv) >= 2:
    PORT = int(sys.argv[1])
if len(sys.argv) >= 3:
    BOOTH_ID = int(sys.argv[2])

PEER_NODES = [5001, 5002, 5003, 5004, 5005, 5006]

from backend.blockchain import Blockchain, Block
from backend.voter_db import VoterAPI, MOCK_VOTERS
import backend.crypto_utils as crypto

app = Flask(__name__, static_folder='static')

# Initialize the global blockchain for the election
election_chain = Blockchain()

# --- Page Routes ---
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

@app.route('/admin')
def serve_admin():
    return send_from_directory('static', 'admin.html')

@app.route('/explorer')
def serve_explorer():
    return send_from_directory('static', 'explorer.html')

@app.route('/commission')
def serve_commission():
    return send_from_directory('static', 'commission.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)


# --- Phase 1 Core API ---
@app.route('/api/authenticate', methods=['POST'])
def authenticate():
    data = request.json
    voter_id = data.get('voter_id')
    input_hash = data.get('fingerprint_hash')

    if not voter_id or not input_hash:
        return jsonify({"status": "error", "message": "Missing voter ID or fingerprint hash"}), 400

    voter_record = VoterAPI.get_voter(voter_id)
    if not voter_record:
        return jsonify({"status": "error", "message": "Voter ID not found in registry"}), 404

    if voter_record["has_voted"]:
        return jsonify({"status": "error", "message": "Voter has already cast a vote (duplicate voting rejected)"}), 403

    if voter_record["assigned_booth"] != BOOTH_ID:
        return jsonify({"status": "error", "message": f"Access Denied: You are registered at Booth {voter_record['assigned_booth']}! This terminal is for Booth {BOOTH_ID}."}), 403

    if input_hash != voter_record["fingerprint_hash"]:
        return jsonify({"status": "error", "message": "Biometric authentication failed"}), 401

    # We no longer mark them as voted here to prevent lockout on power cuts.
    # It will be marked atomically when the block is mined.
    return jsonify({
        "status": "success",
        "message": "Authentication successful. Proceed to private booth."
    }), 200

@app.route('/api/vote', methods=['POST'])
def cast_vote():
    data = request.json
    voter_id = data.get('voter_id')
    vote_vector = data.get('vote_vector')
    fingerprint_hash = data.get('fingerprint_hash')
    
    if not voter_id or not vote_vector or not fingerprint_hash:
        return jsonify({"status": "error", "message": "Missing voter data or fingerprint hash."}), 400

    voter_record = VoterAPI.get_voter(voter_id)
    if not voter_record:
        return jsonify({"status": "error", "message": "Voter not found."}), 404

    if fingerprint_hash != voter_record["fingerprint_hash"]:
        return jsonify({"status": "error", "message": "Fingerprint verification failed."}), 401

    if voter_record["has_voted"]:
        return jsonify({"status": "error", "message": "Voter has already voted."}), 403

    if sum(vote_vector) != 1 or any(v not in [0,1] for v in vote_vector):
        return jsonify({"status": "error", "message": "Invalid vote vector layout."}), 400

    encrypted_vote_objs = crypto.encrypt_vote(vote_vector)
    serialized_enc_vote = crypto.serialize_encrypted_vote(encrypted_vote_objs)

    receipt_id = crypto.generate_receipt_id(voter_id)
    zkp = crypto.generate_zkp(encrypted_vote_objs)

    vote_payload = {
        "receipt_id": receipt_id,
        "encrypted_vote": serialized_enc_vote,
        "zkp": zkp
    }

    if not crypto.verify_zkp(zkp):
        return jsonify({"status": "error", "message": "ZKP Validation failed."}), 400

    last_block = election_chain.last_block
    new_block = Block(index=last_block.index + 1,
                      data=vote_payload,
                      previous_hash=last_block.hash)
    
    proof = election_chain.proof_of_work(new_block)
    election_chain.add_block(new_block, proof)

    # Atomically mark voter as having voted only after chain secures the block
    VoterAPI.mark_as_voted(voter_id)

    # Broadcast block to peer nodes in the network
    def broadcast_block(blk_dict):
        for peer_port in PEER_NODES:
            if peer_port != PORT:
                try:
                    requests.post(f"http://127.0.0.1:{peer_port}/api/nodes/receive_block", json=blk_dict, timeout=1)
                except requests.exceptions.RequestException:
                    pass # Peer offline

    blk_payload = {
        "index": new_block.index, "timestamp": new_block.timestamp,
        "data": new_block.data, "previous_hash": new_block.previous_hash,
        "hash": new_block.hash, "nonce": new_block.nonce
    }
    threading.Thread(target=broadcast_block, args=(blk_payload,)).start()

    return jsonify({
        "status": "success",
        "message": "Vote successfully secured on blockchain.",
        "receipt_id": receipt_id
    }), 200

@app.route('/api/blockchain', methods=['GET'])
def get_blockchain():
    chain_data = []
    for block in election_chain.chain:
        chain_data.append({
            "index": block.index,
            "timestamp": block.timestamp,
            "data": block.data,
            "previous_hash": block.previous_hash,
            "hash": block.hash,
            "nonce": block.nonce
        })
    return jsonify({"length": len(chain_data), "chain": chain_data}), 200


# --- Phase 2 API Enhancements ---

@app.route('/api/voters', methods=['GET'])
def get_voters():
    """Booth Admin endpoint to see local booth voters."""
    voters_list = []
    for vid, record in MOCK_VOTERS.items():
        if record["assigned_booth"] == BOOTH_ID:
            # new: computed identifier hash for dashboard display
            voter_hash = hashlib.sha256(str(vid).encode('utf-8')).hexdigest()
            voters_list.append({
                "voter_id": vid,
                "voter_hash": voter_hash,
                "has_voted": record["has_voted"],
                "fingerprint_hash": record["fingerprint_hash"]
            })
    return jsonify(voters_list), 200

@app.route('/api/validate_chain', methods=['GET'])
def validate_chain():
    """Endpoint for Blockchain Explorer to verify chain integrity."""
    result = election_chain.is_valid_chain()
    return jsonify({"status": "success", "is_valid": result["is_valid"], "broken_block": result["broken_block"], "reason": result["reason"]}), 200

@app.route('/api/hack_block', methods=['POST'])
def hack_block():
    """Hacker simulation: forcefully modify a block's data."""
    data = request.json
    block_index = data.get('block_index')
    new_data = data.get('new_data')
    
    if not block_index or block_index <= 0 or block_index >= len(election_chain.chain):
        return jsonify({"status": "error", "message": "Invalid block index."}), 400
        
    # Introduce the mutation directly
    election_chain.chain[block_index].data = new_data
    return jsonify({"status": "success", "message": f"Block {block_index} data illegally mutated!"}), 200

@app.route('/api/get_private_key', methods=['GET'])
def get_private_key():
    """Retrieves the mock private key for the Election Commission."""
    # A real system would NEVER expose a private key like this, this is purely for the demo dashboard
    p_key_string = f"pk_{crypto.ELECTION_PRIV_KEY.p}_{crypto.ELECTION_PRIV_KEY.q}"
    return jsonify({"private_key": p_key_string}), 200

@app.route('/api/tally', methods=['POST'])
def tally_votes():
    """Homomorphically tally votes securely requiring a Private Key."""
    data = request.json
    provided_key = data.get('private_key')
    actual_key = f"pk_{crypto.ELECTION_PRIV_KEY.p}_{crypto.ELECTION_PRIV_KEY.q}"
    
    if provided_key != actual_key:
        return jsonify({"status": "error", "message": "Access Denied: Invalid Private Key."}), 403

    all_encrypted_votes = []
    for block in election_chain.chain[1:]: # Skip genesis
        payload = block.data
        if "encrypted_vote" in payload:
            try:
                reconstructed_enc = crypto.deserialize_encrypted_vote(payload["encrypted_vote"])
                all_encrypted_votes.append(reconstructed_enc)
            except (ValueError, TypeError, KeyError):
                # Skip corrupted/hacked blocks instead of crashing the entire tally
                continue

    if not all_encrypted_votes:
         return jsonify({"status": "success", "results": [0, 0, 0], "message": "No valid votes found on chain.", "candidates": ["Party A", "Party B", "Party C"]}), 200

    # Homomorphically add
    tally_encrypted = crypto.tally_encrypted_votes(all_encrypted_votes)
    
    # Decrypt final total
    final_tally = crypto.decrypt_tally(tally_encrypted)

    return jsonify({
        "status": "success",
        "candidates": ["Party A", "Party B", "Party C"],
        "results": final_tally
    }), 200

# --- Phase 3: P2P Network Endpoints ---

@app.route('/api/config', methods=['GET'])
def get_config():
    """Returns local node configuration to the frontend."""
    return jsonify({"booth_id": BOOTH_ID, "node_port": PORT}), 200

@app.route('/api/nodes/receive_block', methods=['POST'])
def receive_block():
    """Endpoint for receiving broadcasted blocks from peer booths."""
    block_data = request.json
    
    # Basic consensus validation: ensure the index is higher than our local chain.
    if block_data['index'] > election_chain.last_block.index:
        new_block = Block(block_data['index'], block_data['data'], block_data['previous_hash'])
        new_block.timestamp = block_data['timestamp']
        new_block.hash = block_data['hash']
        new_block.nonce = block_data['nonce']
        
        election_chain.chain.append(new_block)
        return jsonify({"status": "synced", "message": "Block appended from peer"}), 200
        
    return jsonify({"status": "ignored", "message": "Block already exists or invalid"}), 200

if __name__ == '__main__':
    # Threaded mode on, Debug auto-reloader off to prevent interference between 6 instances
    app.run(debug=False, port=PORT, host='0.0.0.0', threaded=True)
