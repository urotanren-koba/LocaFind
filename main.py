# 必要なライブラリのインポート
from fastapi import FastAPI, Query, HTTPException, Request
import requests
from openai import OpenAI
import os
from dotenv import load_dotenv
import logging
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional
from jinja2 import Environment

# ログの設定
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# .envファイルを読み込み
load_dotenv()

# APIキーの取得
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# APIキーの存在確認
if not OPENAI_API_KEY:
    logger.error("OpenAI APIキーが設定されていません")
    raise ValueError("OpenAI APIキーが必要です")

if not GOOGLE_MAPS_API_KEY:
    logger.error("Google Maps APIキーが設定されていません")
    raise ValueError("Google Maps APIキーが必要です")

# OpenAIクライアントの初期化
client = OpenAI(api_key=OPENAI_API_KEY)

# FastAPIアプリの初期化
app = FastAPI()

# 静的ファイルとテンプレートの設定
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# 初期画面のルート設定
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # search.htmlを初期画面として返す
    return templates.TemplateResponse("home.html", {"request": request})

# nl2br フィルタを定義
def nl2br(value):
    return value.replace('\n', '<br>')

# Jinja2 環境にフィルタを登録
templates.env.filters['nl2br'] = nl2br

# 地名から座標を取得する関数
def get_coordinates(location_name):
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={location_name}&key={GOOGLE_MAPS_API_KEY}"
    response = requests.get(url)

    if response.status_code != 200:
        error_msg = f"Google Maps APIから座標を取得できませんでした。ステータスコード: {response.status_code}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

    data = response.json()
    if not data["results"]:
        error_msg = f"{location_name} の座標が見つかりません"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    location = data["results"][0]["geometry"]["location"]
    logger.debug(f"{location_name} の座標: {location}")
    return f"{location['lat']},{location['lng']}"

# Google Maps APIを使って周辺の場所を検索する関数
def search_nearby_places(query: str, location_name: str = None, open_now: Optional[bool] = None, price_level: Optional[int] = None, rating: Optional[float] = None):
    # 地名が指定されている場合、座標を取得
    if location_name:
        location = get_coordinates(location_name)
    else:
        # デフォルトは東京の座標
        location = "35.6895,139.6917"

    logger.debug(f"Google Maps API検索開始: クエリ={query}, 位置={location}")

    params = {
        "location": location,
        "radius": 1500,  # デフォルト値として1500mを設定
        "keyword": query,
        "language": "ja",  # 日本語の結果を優先
        "key": GOOGLE_MAPS_API_KEY
    }

    # オプションパラメータの追加
    if open_now is not None:
        params["opennow"] = open_now
    if price_level is not None:
        params["minprice"] = price_level
        params["maxprice"] = price_level

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    response = requests.get(url, params=params)

    if response.status_code != 200:
        error_msg = f"Google Maps APIリクエストが失敗しました。ステータスコード: {response.status_code}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

    data = response.json()
    results = data["results"]

    # 評価でフィルタリング
    if rating is not None:
        results = [place for place in results if place.get("rating", 0) >= rating]

    if not results:
        error_msg = "該当する場所が見つかりません"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    # 最初の検索結果の場所情報を取得
    place_info = results[0]

    # place_idからウェブサイトのURLを取得して追加
    website_url = get_place_details(place_info["place_id"])
    place_info["website"] = website_url  # 場所情報にウェブサイトURLを追加

    logger.debug(f"検索結果: {place_info}")
    return place_info

# OpenAI APIを使ってチャット形式の応答を生成する関数
def generate_response(place_info):
    logger.debug(f"応答生成開始: 場所情報={place_info}")    
    # 基本情報の取得
    place_name = place_info["name"]
    address = place_info["vicinity"]
    place_types = place_info.get("types", [])  # 場所のタイプを取得 
    # 追加情報の取得
    rating = place_info.get("rating", "未評価")
    ratings_count = place_info.get("user_ratings_total", 0)
    is_open = place_info.get("opening_hours", {}).get("open_now", None)
    open_status = "営業中" if is_open == True else "営業時間外" if is_open == False else "営業時間不明"

    # 場所のタイプに基づいて説明の内容を調整
    place_type_mapping = {
        "school": "学校",
        "restaurant": "飲食店",
        "cafe": "カフェ",
        "tourist_attraction": "観光スポット",
        "park": "公園",
        "museum": "博物館",
        "shopping_mall": "ショッピングモール",
        # ... (他のマッピングは同じまま)
    }   

    # 日本語の場所タイプを特定
    place_types_ja = [place_type_mapping.get(type_, type_) for type_ in place_types if type_ in place_type_mapping]
    place_type_ja = place_types_ja[0] if place_types_ja else "施設"

    # プロンプトの作成
    prompt = f"""以下の情報に基づいて、{place_type_ja}「{place_name}」について魅力的な説明を作成してください：

ユーザーが読みやすいようにその文が終わったら、1行下に下がって新しい文を作成してください。

施設名: {place_name}
種類: {', '.join(place_types_ja) if place_types_ja else '施設'}
住所: {address}
評価: {rating}（{ratings_count}件の評価）
営業状況: {open_status}


文章の始まりは「{place_name}はいかがですか？」
その後の文は簡潔に情報をまとめて、丁寧な言葉遣いで話してください。文章の最後は{place_name}の住所を教えてください"""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": """あなたは最高の観光ガイドです。
                以下のような特徴を踏まえて説明してください：
                - 丁寧な話し方
                - 相手が知りたいことをわかりやすく伝える
                - その場所の特徴や魅力を分かりやすく伝える
                - 具体的で実用的な情報の提供
                - ポジティブながらも正直な評価
                - 時間帯や状況に応じた適切なアドバイス
                - 300文字以内に説明してください
                - 施設の種類に応じた適切な説明と推奨ポイント"""},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500
        )
        response_text = response.choices[0].message.content.strip().replace('\n', ' ')
        # response_text = response.choices[0].message.content.replace('\n', '<br>')

        logger.debug(f"生成された応答: {response_text}")
        return response_text
    except Exception as e:
        error_msg = f"OpenAI APIエラーが発生しました: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/chat")
def chat(
    query: str = Query(..., description="例：近くのカフェや観光スポットを検索"),
    location_name: str = Query(None, description="例：新宿区、葛飾区などの場所を指定"),
    open_now: Optional[bool] = Query(None, description="営業中の施設のみを表示"),
    price_level: Optional[int] = Query(None, description="価格帯（1: 安価 ～ 4: 最高価）"),
    rating: Optional[float] = Query(None, description="最低評価（1.0 ～ 5.0）")
):
    logger.info(f"チャットリクエスト受信: クエリ={query}, 地域={location_name}")
    try:
        place = search_nearby_places(
            query=query,
            location_name=location_name,
            open_now=open_now,
            price_level=price_level,
            rating=rating
        )
        response_text = generate_response(place)
        return {"message": response_text}
    except HTTPException as e:
        logger.error(f"HTTPException発生: {str(e)}")
        raise e
    except Exception as e:
        error_msg = f"予期せぬエラーが発生しました: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/")
def read_root():
    return {"message": "サーバーが正常に動作しています"}

@app.get("/search", response_class=HTMLResponse)
async def search_form(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})

@app.get("/results", response_class=HTMLResponse)
async def display_results(
    request: Request,
    query: str,
    location_name: str,
    open_now: Optional[str] = None,
    price_level: Optional[str] = None,
    rating: Optional[str] = None
):
    try:
        # パラメータの型変換と検証
        open_now = True if open_now == "true" else None
        price_level = int(price_level) if price_level and price_level.strip() else None
        rating = float(rating) if rating and rating.strip() else None
        
        # 検索結果と応答の生成
        place = search_nearby_places(
            query=query,
            location_name=location_name,
            open_now=open_now,
            price_level=price_level,
            rating=rating
        )
        response_text = generate_response(place)
        
        # place_info（place）を展開し、place_nameとaddressも渡します
        place_name = place["name"]
        address = place["vicinity"]
        
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "response_text": response_text,
                "place_info": place,
                "place_name": place_name,
                "address": address
            }
        )
    except HTTPException as e:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "error": str(e.detail)
            }
        )

# Google Maps APIを使って周辺の場所を検索し、詳細情報を取得する関数
def get_place_details(place_id):
    url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&key={GOOGLE_MAPS_API_KEY}"
    response = requests.get(url)

    if response.status_code != 200:
        logger.error(f"Google Maps Place Details APIリクエストが失敗しました。ステータスコード: {response.status_code}")
        return None

    data = response.json()
    if data.get("result") and "website" in data["result"]:
        return data["result"]["website"]
    return None
