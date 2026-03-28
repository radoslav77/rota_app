# 🍽 Rota Manager – Django App

A Django application for generating and managing kitchen staff rotas with a self-learning shift pattern algorithm.

## Features

- **Import** existing `.xlsx` rota files (your exact format is supported)
- **Self-learning algorithm** that learns shift patterns from historical data per staff per day-of-week
- **Generate new rotas** with holiday/day-off inputs, auto-filled by the algorithm
- **View & edit** rotas in a colour-coded table (click any cell to edit)
- **Export** generated rotas back to `.xlsx` in the same format as the original file
- **Staff pattern viewer** – see what the algorithm has learned for each staff member

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run database migrations
```bash
python manage.py migrate
```

### 3. Start the server
```bash
python manage.py runserver
```

Then open **http://127.0.0.1:8000/** in your browser.

---

## How to use

### Step 1 – Import your rota
1. Go to **Import** in the top navigation
2. Upload your `Copy_of_Rota_Feb_full__2026.xlsx` (or any rota in the same format)
3. The system parses all rota sheets, saves staff/sections/shifts, and **learns shift patterns**

### Step 2 – Generate a new rota
1. Go to **Generate**
2. Set the label, start date, and end date (up to 14 days)
3. Click **Build Holiday Grid** to see the staff grid
4. For each staff member, set `H` (holiday), `OFF`, or `SICK` on specific days
5. Leave cells as `auto` — the algorithm will fill these in
6. Click **Generate Rota**

### Step 3 – Review & edit
- Click any cell in the rota view to edit a shift value
- Changes are saved instantly via AJAX
- Corrections also teach the algorithm

### Step 4 – Export
- Click **Export Excel** on any rota view to download a `.xlsx` file in the original format

---

## Self-Learning Algorithm

The algorithm (`rota/ml/algorithm.py`) works as follows:

1. **Learning phase**: After importing historical rotas, it counts how often each staff member works each shift on each day of the week (stored in `ShiftPattern` table)

2. **Generation phase**: For a new period:
   - Respects all pre-set holidays/off days you enter
   - Ensures **minimum 2 days off per week** per staff member (prefers days they historically have off)
   - Fills remaining days with the **most frequent shift** for that staff+day combination
   - Falls back to the staff member's overall most common shift if no day-specific pattern exists

3. **Continuous learning**: Manual edits to the rota are also used to improve future predictions

---

## Project Structure

```
rota_app/
├── manage.py
├── requirements.txt
├── rota_project/        # Django project config
│   ├── settings.py
│   └── urls.py
└── rota/                # Main app
    ├── models.py        # Section, Staff, RotaPeriod, ShiftEntry, ShiftPattern
    ├── views.py         # Dashboard, import, generate, view, export, AJAX edit
    ├── urls.py
    ├── excel_parser.py  # Reads .xlsx files in your rota format
    ├── excel_export.py  # Writes .xlsx files matching the original format
    ├── ml/
    │   └── algorithm.py # Self-learning shift pattern algorithm
    ├── templates/rota/  # HTML templates
    └── templatetags/    # Custom Django template filters
```
