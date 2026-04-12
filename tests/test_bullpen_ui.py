from core.mlb_client import BullpenData

import sys
def test_display():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    dates = ["4/8", "4/9", "4/10", "4/11"]
    bullpen = [
        {"name": "Finnegan", "t": "R", "era": "2.45", "4/8": "15", "4/9": "", "4/10": "12", "4/11": ""}, # Fresh
        {"name": "Harvey", "t": "R", "era": "3.12", "4/8": "", "4/9": "24", "4/10": "18", "4/11": ""}, # Tired (back to back or usage) - wait counts[-1] is 4/11. 
        # Indices: [4/8(0), 4/9(1), 4/10(2), 4/11(3)]
        # Harvey: [0, 24, 18, 0] -> yest=0, day_before=18, day_3=24. Total=42. Status: Used (Yellow)
        {"name": "Law", "t": "R", "era": "4.56", "4/8": "10", "4/9": "12", "4/10": "15", "4/11": "20"}, # Gassed (4 in a row)
        {"name": "Rainey", "t": "R", "era": "6.12", "4/8": "", "4/9": "", "4/10": "", "4/11": "35"}, # Tired (35 yest)
    ]
    starters = [
        {"name": "Irvin", "t": "R", "era": "3.88", "4/8": "95", "4/9": "", "4/10": "", "4/11": ""},
    ]
    
    bd = BullpenData("Washington Nationals", dates, bullpen, starters)
    print(bd.format_table())

if __name__ == "__main__":
    test_display()
