#!/usr/bin/env python3
"""
Template Rects Calculator
ฟังก์ชันคำนวณตำแหน่งและขนาดของ rects สำหรับแต่ละ template
รวมถึงเครื่องมือวัด rects จากภาพจริงด้วยการลากกรอบ
"""

import json
import cv2
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
import os


def calculate_single_slot_rects(
    left_pct: float = 1.6,
    top_pct: float = 25.35,
    width_pct: float = 97.0,
    height_pct: float = 38.5
) -> List[Dict[str, float]]:
    """
    คำนวณ rects สำหรับ template ที่มี 1 slot
    
    Args:
        left_pct: เปอร์เซ็นต์ตำแหน่งซ้าย (default: 1.6)
        top_pct: เปอร์เซ็นต์ตำแหน่งบน (default: 25.35)
        width_pct: เปอร์เซ็นต์ความกว้าง (default: 97.0)
        height_pct: เปอร์เซ็นต์ความสูง (default: 38.5)
    
    Returns:
        List ของ rect objects
    """
    return [{
        "leftPct": left_pct,
        "topPct": top_pct,
        "widthPct": width_pct,
        "heightPct": height_pct
    }]


def calculate_two_slots_rects(
    left_pct: float = 6.0,
    top_pct: float = 10.0,
    width_pct: float = 88.0,
    height_pct: float = 40.0,
    vertical_gap_pct: float = 2.0
) -> List[Dict[str, float]]:
    """
    คำนวณ rects สำหรับ template ที่มี 2 slots (เรียงแนวตั้ง)
    
    Args:
        left_pct: เปอร์เซ็นต์ตำแหน่งซ้าย (default: 6.0)
        top_pct: เปอร์เซ็นต์ตำแหน่งบนของ slot แรก (default: 10.0)
        width_pct: เปอร์เซ็นต์ความกว้าง (default: 88.0)
        height_pct: เปอร์เซ็นต์ความสูง (default: 40.0)
        vertical_gap_pct: เปอร์เซ็นต์ช่องว่างระหว่าง slots (default: 2.0)
    
    Returns:
        List ของ rect objects
    """
    rects = []
    
    # Slot แรก
    rects.append({
        "leftPct": left_pct,
        "topPct": top_pct,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    # Slot ที่สอง (อยู่ด้านล่าง)
    second_top = top_pct + height_pct + vertical_gap_pct
    rects.append({
        "leftPct": left_pct,
        "topPct": second_top,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    return rects


def calculate_four_slots_rects(
    left_pct: float = 6.0,
    top_pct: float = 12.0,
    width_pct: float = 41.0,
    height_pct: float = 32.0,
    horizontal_gap_pct: float = 6.0,
    vertical_gap_pct: float = 10.0
) -> List[Dict[str, float]]:
    """
    คำนวณ rects สำหรับ template ที่มี 4 slots (2x2 grid)
    
    Args:
        left_pct: เปอร์เซ็นต์ตำแหน่งซ้ายของ column แรก (default: 6.0)
        top_pct: เปอร์เซ็นต์ตำแหน่งบนของ row แรก (default: 12.0)
        width_pct: เปอร์เซ็นต์ความกว้าง (default: 41.0)
        height_pct: เปอร์เซ็นต์ความสูง (default: 32.0)
        horizontal_gap_pct: เปอร์เซ็นต์ช่องว่างระหว่าง columns (default: 6.0)
        vertical_gap_pct: เปอร์เซ็นต์ช่องว่างระหว่าง rows (default: 10.0)
    
    Returns:
        List ของ rect objects
    """
    rects = []
    
    # คำนวณตำแหน่งของ column ที่สอง
    second_left = left_pct + width_pct + horizontal_gap_pct
    
    # คำนวณตำแหน่งของ row ที่สอง
    second_top = top_pct + height_pct + vertical_gap_pct
    
    # Slot 1 (ซ้ายบน)
    rects.append({
        "leftPct": left_pct,
        "topPct": top_pct,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    # Slot 2 (ขวาบน)
    rects.append({
        "leftPct": second_left,
        "topPct": top_pct,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    # Slot 3 (ซ้ายล่าง)
    rects.append({
        "leftPct": left_pct,
        "topPct": second_top,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    # Slot 4 (ขวาล่าง)
    rects.append({
        "leftPct": second_left,
        "topPct": second_top,
        "widthPct": width_pct,
        "heightPct": height_pct
    })
    
    return rects


def calculate_full_screen_rects() -> List[Dict[str, float]]:
    """
    คำนวณ rects สำหรับ template ที่เต็มหน้าจอ (ไม่มี background)
    
    Returns:
        List ของ rect objects (เต็มหน้าจอ)
    """
    return [{
        "leftPct": 0.0,
        "topPct": 0.0,
        "widthPct": 100.0,
        "heightPct": 100.0
    }]


def calculate_rects_by_template_id(template_id: str) -> List[Dict[str, float]]:
    """
    คำนวณ rects ตาม template ID
    
    Args:
        template_id: ID ของ template
    
    Returns:
        List ของ rect objects
    """
    template_configs = {
        "template1": {
            "function": calculate_single_slot_rects,
            "params": {"left_pct": 1.6, "top_pct": 25.35, "width_pct": 97.0, "height_pct": 38.5}
        },
        "template2_two_slots": {
            "function": calculate_two_slots_rects,
            "params": {"left_pct": 6.0, "top_pct": 10.0, "width_pct": 88.0, "height_pct": 40.0}
        },
        "template4_four_slots": {
            "function": calculate_four_slots_rects,
            "params": {"left_pct": 6.0, "top_pct": 12.0, "width_pct": 41.0, "height_pct": 32.0}
        },
        "single_full": {
            "function": calculate_full_screen_rects,
            "params": {}
        }
    }
    
    if template_id not in template_configs:
        raise ValueError(f"Template ID '{template_id}' not found. Available: {list(template_configs.keys())}")
    
    config = template_configs[template_id]
    return config["function"](**config["params"])


def calculate_rects_by_slots(slots: int, custom_params: Dict[str, Any] = None) -> List[Dict[str, float]]:
    """
    คำนวณ rects ตามจำนวน slots
    
    Args:
        slots: จำนวน slots ที่ต้องการ
        custom_params: พารามิเตอร์เพิ่มเติมสำหรับการคำนวณ
    
    Returns:
        List ของ rect objects
    """
    if custom_params is None:
        custom_params = {}
    
    if slots == 1:
        return calculate_single_slot_rects(**custom_params)
    elif slots == 2:
        return calculate_two_slots_rects(**custom_params)
    elif slots == 4:
        return calculate_four_slots_rects(**custom_params)
    else:
        raise ValueError(f"Unsupported number of slots: {slots}. Supported: 1, 2, 4")


def load_templates_from_json(json_path: str) -> List[Dict[str, Any]]:
    """
    โหลด templates จากไฟล์ JSON
    
    Args:
        json_path: path ไปยังไฟล์ JSON
    
    Returns:
        List ของ template objects
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def update_template_rects(template: Dict[str, Any]) -> Dict[str, Any]:
    """
    อัปเดต rects ของ template โดยใช้ฟังก์ชันคำนวณ
    
    Args:
        template: template object
    
    Returns:
        template object ที่มี rects ถูกคำนวณใหม่
    """
    template_id = template.get('id', '')
    slots = template.get('slots', 1)
    
    # ถ้า template มี rects อยู่แล้ว ให้ใช้ค่าเดิม
    if 'rects' in template and template['rects']:
        return template
    
    # คำนวณ rects ใหม่
    try:
        if template_id in ['template1', 'template2_two_slots', 'template4_four_slots', 'single_full']:
            rects = calculate_rects_by_template_id(template_id)
        else:
            rects = calculate_rects_by_slots(slots)
        
        template['rects'] = rects
    except Exception as e:
        print(f"Warning: Could not calculate rects for template '{template_id}': {e}")
        # ใช้ default single slot ถ้าไม่สามารถคำนวณได้
        template['rects'] = calculate_single_slot_rects()
    
    return template


def print_rects_info(rects: List[Dict[str, float]], template_name: str = ""):
    """
    แสดงข้อมูล rects ในรูปแบบที่อ่านง่าย
    
    Args:
        rects: List ของ rect objects
        template_name: ชื่อของ template
    """
    if template_name:
        print(f"\n=== {template_name} ===")
    
    for i, rect in enumerate(rects, 1):
        print(f"Slot {i}:")
        print(f"  Left: {rect['leftPct']}%")
        print(f"  Top: {rect['topPct']}%")
        print(f"  Width: {rect['widthPct']}%")
        print(f"  Height: {rect['heightPct']}%")
        print()


class RectMeasurer:
    """
    คลาสสำหรับวัด rects จากภาพจริงด้วยการลากกรอบ
    """
    
    def __init__(self):
        self.drawing = False
        self.start_point = None
        self.end_point = None
        self.rects = []
        self.current_rect = None
        self.image = None
        self.image_copy = None
        self.window_name = "Rect Measurer - Drag to measure rectangles"
        
    def mouse_callback(self, event, x, y, flags, param):
        """
        Callback function สำหรับการจัดการ mouse events
        """
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drawing = True
            self.start_point = (x, y)
            self.current_rect = None
            
        elif event == cv2.EVENT_MOUSEMOVE:
            if self.drawing:
                self.end_point = (x, y)
                # อัปเดตภาพแสดงกรอบที่กำลังลาก
                self.image_copy = self.image.copy()
                if self.start_point and self.end_point:
                    cv2.rectangle(self.image_copy, self.start_point, self.end_point, (0, 255, 0), 2)
                
                # แสดง rects ที่วัดแล้ว
                for i, rect in enumerate(self.rects):
                    cv2.rectangle(self.image_copy, rect['start'], rect['end'], (255, 0, 0), 2)
                    cv2.putText(self.image_copy, f"Rect {i+1}", 
                              (rect['start'][0], rect['start'][1]-10), 
                              cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
                
                cv2.imshow(self.window_name, self.image_copy)
                
        elif event == cv2.EVENT_LBUTTONUP:
            self.drawing = False
            self.end_point = (x, y)
            
            if self.start_point and self.end_point:
                # สร้าง rect ใหม่
                self.current_rect = {
                    'start': self.start_point,
                    'end': self.end_point,
                    'left': min(self.start_point[0], self.end_point[0]),
                    'top': min(self.start_point[1], self.end_point[1]),
                    'right': max(self.start_point[0], self.end_point[0]),
                    'bottom': max(self.start_point[1], self.end_point[1])
                }
                
                # คำนวณ width และ height
                self.current_rect['width'] = self.current_rect['right'] - self.current_rect['left']
                self.current_rect['height'] = self.current_rect['bottom'] - self.current_rect['top']
                
                # เพิ่ม rect เข้าไปในรายการ
                self.rects.append(self.current_rect.copy())
                
                print(f"Rect {len(self.rects)} added:")
                print(f"  Position: ({self.current_rect['left']}, {self.current_rect['top']})")
                print(f"  Size: {self.current_rect['width']} x {self.current_rect['height']}")
                
                # อัปเดตภาพแสดง rects ทั้งหมด
                self.image_copy = self.image.copy()
                for i, rect in enumerate(self.rects):
                    cv2.rectangle(self.image_copy, rect['start'], rect['end'], (255, 0, 0), 2)
                    cv2.putText(self.image_copy, f"Rect {i+1}", 
                              (rect['start'][0], rect['start'][1]-10), 
                              cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
                
                cv2.imshow(self.window_name, self.image_copy)
    
    def measure_rects_from_image(self, image_path: str, max_width: int = 1200) -> List[Dict[str, float]]:
        """
        วัด rects จากภาพจริง
        
        Args:
            image_path: path ไปยังไฟล์ภาพ
            max_width: ความกว้างสูงสุดของหน้าต่างแสดงผล
        
        Returns:
            List ของ rect objects ในรูปแบบเปอร์เซ็นต์
        """
        # โหลดภาพ
        self.image = cv2.imread(image_path)
        if self.image is None:
            raise ValueError(f"Could not load image: {image_path}")
        
        # ปรับขนาดภาพถ้าใหญ่เกินไป
        height, width = self.image.shape[:2]
        if width > max_width:
            scale = max_width / width
            new_width = max_width
            new_height = int(height * scale)
            self.image = cv2.resize(self.image, (new_width, new_height))
            height, width = new_height, new_width
        
        self.image_copy = self.image.copy()
        
        # สร้างหน้าต่างและตั้งค่า mouse callback
        cv2.namedWindow(self.window_name, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(self.window_name, self.mouse_callback)
        
        print("=== Rect Measurer ===")
        print("Instructions:")
        print("- Drag mouse to create rectangles")
        print("- Press 'r' to reset all rectangles")
        print("- Press 'd' to delete last rectangle")
        print("- Press 'c' to calculate percentages")
        print("- Press 's' to save rects to JSON")
        print("- Press 'q' to quit")
        print("- Press ESC to quit")
        print()
        
        while True:
            cv2.imshow(self.window_name, self.image_copy)
            key = cv2.waitKey(1) & 0xFF
            
            if key == ord('q') or key == 27:  # 'q' or ESC
                break
            elif key == ord('r'):  # Reset
                self.rects = []
                self.image_copy = self.image.copy()
                print("All rectangles reset")
            elif key == ord('d'):  # Delete last
                if self.rects:
                    self.rects.pop()
                    self.image_copy = self.image.copy()
                    for i, rect in enumerate(self.rects):
                        cv2.rectangle(self.image_copy, rect['start'], rect['end'], (255, 0, 0), 2)
                        cv2.putText(self.image_copy, f"Rect {i+1}", 
                                  (rect['start'][0], rect['start'][1]-10), 
                                  cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
                    print(f"Last rectangle deleted. {len(self.rects)} rectangles remaining")
                else:
                    print("No rectangles to delete")
            elif key == ord('c'):  # Calculate percentages
                self.calculate_percentages(width, height)
            elif key == ord('s'):  # Save to JSON
                self.save_rects_to_json()
        
        cv2.destroyAllWindows()
        
        # คำนวณเปอร์เซ็นต์และส่งคืน
        return self.calculate_percentages(width, height, return_result=True)
    
    def calculate_percentages(self, image_width: int, image_height: int, return_result: bool = False) -> Optional[List[Dict[str, float]]]:
        """
        คำนวณเปอร์เซ็นต์ของ rects
        
        Args:
            image_width: ความกว้างของภาพ
            image_height: ความสูงของภาพ
            return_result: ถ้า True จะส่งคืนผลลัพธ์
        
        Returns:
            List ของ rect objects ในรูปแบบเปอร์เซ็นต์
        """
        if not self.rects:
            print("No rectangles to calculate")
            return [] if return_result else None
        
        rects_pct = []
        
        print(f"\n=== Calculated Percentages (Image: {image_width}x{image_height}) ===")
        for i, rect in enumerate(self.rects):
            left_pct = (rect['left'] / image_width) * 100
            top_pct = (rect['top'] / image_height) * 100
            width_pct = (rect['width'] / image_width) * 100
            height_pct = (rect['height'] / image_height) * 100
            
            rect_pct = {
                "leftPct": round(left_pct, 2),
                "topPct": round(top_pct, 2),
                "widthPct": round(width_pct, 2),
                "heightPct": round(height_pct, 2)
            }
            
            rects_pct.append(rect_pct)
            
            print(f"Rect {i+1}:")
            print(f"  leftPct: {rect_pct['leftPct']}")
            print(f"  topPct: {rect_pct['topPct']}")
            print(f"  widthPct: {rect_pct['widthPct']}")
            print(f"  heightPct: {rect_pct['heightPct']}")
            print()
        
        if return_result:
            return rects_pct
        return None
    
    def save_rects_to_json(self, filename: str = "measured_rects.json"):
        """
        บันทึก rects ลงไฟล์ JSON
        
        Args:
            filename: ชื่อไฟล์ที่จะบันทึก
        """
        if not self.rects:
            print("No rectangles to save")
            return
        
        # คำนวณเปอร์เซ็นต์
        height, width = self.image.shape[:2]
        rects_pct = self.calculate_percentages(width, height, return_result=True)
        
        # สร้าง template object
        template = {
            "id": "measured_template",
            "name": "Measured Template",
            "slots": len(rects_pct),
            "background": "path/to/your/template.png",
            "vintage_effect": False,
            "rects": rects_pct
        }
        
        # บันทึกลงไฟล์
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
        
        print(f"Rects saved to {filename}")
        print("You can copy the 'rects' array to your template configuration")


def measure_template_rects(image_path: str) -> List[Dict[str, float]]:
    """
    ฟังก์ชันสำหรับวัด rects จากภาพ template
    
    Args:
        image_path: path ไปยังไฟล์ภาพ template
    
    Returns:
        List ของ rect objects ในรูปแบบเปอร์เซ็นต์
    """
    measurer = RectMeasurer()
    return measurer.measure_rects_from_image(image_path)


def main():
    """
    ฟังก์ชันหลักสำหรับทดสอบการทำงาน
    """
    print("=== Template Rects Calculator ===\n")
    
    # ตรวจสอบว่ามีการส่ง argument สำหรับการวัด rects หรือไม่
    import sys
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        if os.path.exists(image_path):
            print(f"Measuring rects from image: {image_path}")
            try:
                rects = measure_template_rects(image_path)
                print(f"\nMeasurement completed! Found {len(rects)} rectangles.")
            except Exception as e:
                print(f"Error measuring rects: {e}")
        else:
            print(f"Image file not found: {image_path}")
        return
    
    # ทดสอบการคำนวณ rects สำหรับแต่ละ template type
    templates_to_test = [
        ("template1", "Single Slot Template"),
        ("template2_two_slots", "Two Slots Template"),
        ("template4_four_slots", "Four Slots Template"),
        ("single_full", "Full Screen Template")
    ]
    
    for template_id, template_name in templates_to_test:
        try:
            rects = calculate_rects_by_template_id(template_id)
            print_rects_info(rects, template_name)
        except Exception as e:
            print(f"Error calculating {template_name}: {e}\n")
    
    # ทดสอบการคำนวณตามจำนวน slots
    print("\n=== Testing by Number of Slots ===")
    for slots in [1, 2, 4]:
        try:
            rects = calculate_rects_by_slots(slots)
            print_rects_info(rects, f"{slots} Slots Template")
        except Exception as e:
            print(f"Error calculating {slots} slots: {e}\n")
    
    # ทดสอบการโหลดและอัปเดต templates จาก JSON
    print("\n=== Testing JSON Template Loading ===")
    try:
        json_path = "public/templates/index.json"
        templates = load_templates_from_json(json_path)
        
        print(f"Loaded {len(templates)} templates from {json_path}")
        
        # อัปเดต rects สำหรับ templates ที่ไม่มี rects
        updated_templates = []
        for template in templates:
            updated_template = update_template_rects(template.copy())
            updated_templates.append(updated_template)
        
        # แสดงผลลัพธ์
        for template in updated_templates:
            if 'rects' in template:
                print_rects_info(template['rects'], f"{template.get('name', template.get('id', 'Unknown'))}")
        
    except FileNotFoundError:
        print(f"JSON file not found: {json_path}")
    except Exception as e:
        print(f"Error loading templates: {e}")
    
    print("\n=== Usage for Measuring Rects ===")
    print("To measure rects from an image:")
    print("  python3 calculate.py path/to/your/template.png")
    print("\nExample:")
    print("  python3 calculate.py templates/template1.png")


if __name__ == "__main__":
    main()
