from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sslify import SSLify

from transformers import AutoModelForCausalLM, AutoTokenizer
from torch import torch
from datetime import datetime
from collections import deque
from dotenv import load_dotenv

import threading
import gdown
from zipfile import ZipFile
import psycopg2
import os

app = Flask(__name__)
CORS(app, supports_credentials=True)
sslify = SSLify(app)

load_dotenv() 


history_list = {}
MODEL_LIST = {}

try:
    conn = psycopg2.connect(
        host=os.getenv('host'),
        port=os.getenv('port'),
        database=os.getenv('db'), 
        user=os.getenv('user'),    
        password=os.getenv('pass')
    )

    cursor = conn.cursor()
    print(conn.get_dsn_parameters(),"\n")

    cursor.execute("SELECT version();")
    record = cursor.fetchone()
    print("You are connected into the - ", record, "\n")

except(Exception, psycopg2.Error) as error:
    print("Error connecting to PostgreSQL database", error)
    conn = None

cursor.execute("SELECT * FROM model")
all_models = cursor.fetchall()

for i in all_models:

    model_dir = ""

    if i[2] != '' and i[2] is not None:
        url =  i[2]
        output = str(i[3]) + '.zip'
        gdown.download(url, output, quiet=False)

        with ZipFile(output, 'r') as zipObj:
            zipObj.extractall()

        model_dir = "content/"
    
    tokenizer = AutoTokenizer.from_pretrained(model_dir + i[3])
    model = AutoModelForCausalLM.from_pretrained(model_dir + i[4])

    MODEL_LIST[i[0]] = {"name": i[1], "tokenizer": tokenizer , "model": model }

def kill_token(token):
    try:
        del history_list[token]
        print("A Token Expired")
    except(Exception, KeyError) as error:
        print("Token does not exist anymore")


def generate_response(tokenizer, model, chat_round, context, query, token, parameters):

    new_input_ids = tokenizer.encode(query + tokenizer.eos_token, return_tensors='pt')

    if token is None: # first time messaging
        print("- REGISTERING TOKEN -")
        q = deque(maxlen=4) 
        q.append(new_input_ids)

        token = str(datetime.now().strftime("%Y%m%d%H%M%S"))
        threading.Timer(600.0, kill_token, [token]).start() # This will clear the memory for token

    else: # reuse previous n lines of context
        print("- ACCESSING TOKEN -")
        q = context

        q.append(new_input_ids)
        chat_history_ids = torch.cat([line for line in q], dim=-1)

    bot_input_ids = chat_history_ids if chat_round > 0 else new_input_ids
    
    all_chat_ids = model.generate(bot_input_ids, pad_token_id=tokenizer.eos_token_id, 
                                                max_length=parameters["max_length"],
                                                do_sample=parameters["do_sample"],
                                                top_k=parameters["top_k"],
                                                top_p=parameters["top_p"],
                                                temperature=parameters["temperature"],
                                                repetition_penalty=parameters["repetition_penalty"]
                                                )
    
    bot_output_ids = all_chat_ids[:, bot_input_ids.shape[-1]:]

    q.append(bot_output_ids) # add bot reply to q

    reply = tokenizer.decode(all_chat_ids[:, bot_input_ids.shape[-1]:][0], skip_special_tokens=True)    

    history_list[token] = ({"context": q, "chat_round": chat_round + 1}) # store in memory to remember context

    if chat_round > 0:    
        return {"response": reply, "chat_round": chat_round + 1, "parameters": parameters}
    else:
        return {"response": reply, "token": token, "parameters": parameters}

@app.route("/generate", methods=["GET"])
def generate():

    # required
    query = request.args.get('query')
    token = request.args.get('token')
    model_id = int(request.args.get('model_id'))

    # sampling parameters
    parameters = {}
    parameters["max_length"] = int(request.args.get("max_length", default=1000))
    parameters["do_sample"] = bool(request.args.get("do_sample", default=False))
    parameters["top_k"] = int(request.args.get("top_k", default=50))
    parameters["top_p"] = int(request.args.get("top_p", default=1))
    parameters["temperature"] = float(request.args.get("temperature", default=1.0))
    parameters["repetition_penalty"]  = float(request.args.get("repetition_penalty ", default=1.0))

    if query is None or model_id is None:
        return {"error": "query or model_id parameter missing"}

    model_set = MODEL_LIST.get(model_id)  

    if model_set is None:
        return {"error": "Model does not exist"}

    # First time messaging
    if token not in history_list:
        return generate_response(model_set["tokenizer"], model_set["model"], 0, None, query, None, parameters)
    else:
        history = history_list.get(token)
        return generate_response(model_set["tokenizer"], model_set["model"], history["chat_round"],  history["context"], query, token, parameters)


@app.route("/get_models", methods=["GET"])
def get_models():

    lis = {}
    for key in MODEL_LIST.keys():
        lis[key] = MODEL_LIST.get(key)["name"]

    return {"models": lis}

    
@app.route("/submit_rating", methods = ["POST"])
def submit_rating():
    
    conversation = request.args.get('conversation')
    rating = request.args.get('rating')
    model_id = request.args.get('model_id')

    if conversation is None or rating is None or model_id is None:
        return {"error": "model_id, rating, and conversation parameter is required"}
    

    try:
        print("- SUBMITTING RATING -")
        cursor.execute("INSERT INTO model_ratings (conversation, rating, model_used) VALUES (%s, %s, %s)", (conversation, rating, model_id))
        conn.commit()
        return {"success": "Successfully added"}
    # Handle the error throws by the command that is useful when using Python while working with PostgreSQL
    except(Exception, psycopg2.Error) as error:
        print("DB ERROR", error)
        return {"error": "Could not add to DB"}


@app.route("/get_ratings", methods=["GET"])
def get_ratings():

    model_id = request.args.get('model_id')

    if model_id is None or model_id == "":
        return {"error": "model_id parameter is required"}

    try:
        cursor.execute("SELECT * FROM model_ratings WHERE model_used = " + model_id)
        all_ratings = cursor.fetchall()

        lis = {}
        for rating in all_ratings:
            lis[rating[0]] = {"conversation": rating[1], "rating": rating[2], "model_used": rating[3]}

        return {"ratings": lis}

    except(Exception, psycopg2.Error) as error:
        print(error)
        return {"error": "Model does not exist"}

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))