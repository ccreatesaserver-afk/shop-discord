# 🏪 DiscordForge Store — v4.0.0

> **חנות תבניות ובוטים מקצועית לדיסקורד**  
> אתר + בוט מסונכרנים עם Firebase

---

## 📁 מבנה הפרויקט

```
store/
├── index.html          ← האתר המלא (one-file)
├── bot.py              ← בוט Discord v4.0
├── firebase-key.json   ← מפתח Firebase (הוסף בעצמך)
├── requirements.txt    ← Python dependencies
└── README.md
```

---

## 🔧 הגדרות נדרשות

### 1. Firebase
1. צור פרויקט ב-[console.firebase.google.com](https://console.firebase.google.com)
2. הפעל **Realtime Database** (mode: test)
3. צור **Service Account** → הורד `firebase-key.json`
4. הכנס את ה-`firebase-key.json` לתיקיה
5. עדכן את ה-URL בשני הקבצים:
   - `bot.py`: `FIREBASE_DB_URL`
   - `index.html`: `firebaseConfig.databaseURL`

### 2. Firebase Auth (לאתר)
1. ב-Firebase Console → Authentication → Sign-in methods
2. הפעל: **Email/Password** + **Google**
3. הוסף את הדומיין שלך ל-Authorized domains
4. עדכן ב-`index.html` → `firebaseConfig`:
   ```js
   apiKey: "YOUR_API_KEY",
   authDomain: "YOUR_PROJECT.firebaseapp.com",
   // ...
   ```

### 3. Discord Bot
1. צור בוט ב-[discord.com/developers](https://discord.com/developers)
2. הפעל: **Privileged Intents** (Server Members + Message Content + Presence)
3. הוסף הרשאות: `Administrator` (לבוט)
4. הגדר משתני סביבה:
   ```env
   BOT_TOKEN=your_token_here
   FIREBASE_DB_URL=https://your-project-default-rtdb.firebaseio.com
   ```

### 4. הפעלת הבוט
```bash
pip install -r requirements.txt
python bot.py
```

### 5. Deploy האתר
- **GitHub Pages** (חינמי): העלה את `index.html`
- **Vercel** / **Netlify**: גרור ושחרר
- **VPS**: כל web server פשוט

---

## 🔄 תהליך הרכישה (Flow)

```
לקוח רוכש באתר
    ↓
Firebase שומר: purchases/{uid}/{token} + tokens/{token}
    ↓
לקוח מקבל invite link עם token
    ↓
לקוח מזמין בוט לשרת
    ↓
on_guild_join → בוט מוצא token פתוח
    ↓
בוט מוחק מבנה ישן + בונה תבנית
    ↓
בוט מסמן token כ-used
    ↓
בוט שולח הודעת הצלחה + יוצא אוטומטית ✨
```

---

## 🗃️ מבנה Firebase

```
/
├── tokens/
│   └── {TOKEN}/
│       ├── productId: "gaming-pro"
│       ├── userId: "firebase_uid"
│       ├── used: false
│       └── usedAt: "2025-..."
│
├── purchases/
│   └── {USER_ID}/
│       └── {TOKEN}/
│           ├── title: "Gaming Pro Server"
│           ├── price: 29
│           ├── purchasedAt: "..."
│           ├── inviteLink: "..."
│           └── status: "active"
│
├── templates/
│   └── {TEMPLATE_ID}/
│       ├── description: "..."
│       ├── roles: [...]
│       └── categories: [...]
│
├── audit/
│   └── {GUILD_ID}/
│       └── {PUSH_ID}/
│           ├── action: "COPY"
│           └── ...
│
└── store_audit/
    └── {GUILD_ID}/
        └── {PUSH_ID}/
```

---

## 💰 הוספת מוצרים

### באתר (`index.html`)
ערוך את מערך `PRODUCTS`:
```js
{
  id: "my-template",
  category: "template",  // template | bot | bundle
  title: "שם המוצר",
  desc: "תיאור קצר",
  emoji: "🎮",
  price: 39,
  originalPrice: 59,  // null לביטול קו חוצה
  badge: "new",       // hot | new | sale | bot | bundle | null
  tags: ["tag1", "tag2"],
  features: ["פיצ׳ר 1", "פיצ׳ר 2"],
  stars: 5,
  reviews: 42
}
```

### בבוט (`bot.py`)
הוסף ל-`TEMPLATE_MAP`:
```python
"my-template": "tpl_my_template",
```

ואז צור את התבנית ב-Firebase:
```
templates/tpl_my_template/...
```
או הוסף ל-`get_default_template()` built-in.

---

## 🛡️ Firebase Security Rules (Production)

```json
{
  "rules": {
    "tokens": {
      ".read": false,
      ".write": false,
      "$token": {
        ".read": "auth != null && data.child('userId').val() === auth.uid",
        ".write": "auth != null && data.child('userId').val() === auth.uid"
      }
    },
    "purchases": {
      "$uid": {
        ".read": "auth != null && auth.uid === $uid",
        ".write": "auth != null && auth.uid === $uid"
      }
    },
    "templates": {
      ".read": true,
      ".write": false
    }
  }
}
```

---

## 📞 תמיכה

> בנוי עם ❤️ ל-DiscordForge | v4.0.0
