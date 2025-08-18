# Additional Implementation Files

## 1. **src/agents/extraction_agent.py**
```python
from langchain_openai import ChatOpenAI
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import PromptTemplate
from typing import List, Optional, Dict, Any
import json

from ..models.document_schemas import (
    DocumentType, ExtractedField, InvoiceSchema, 
    MedicalBillSchema, PrescriptionSchema
)
from ..utils.prompt_templates import EXTRACTION_PROMPTS

class ExtractionAgent:
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.extraction_prompts = EXTRACTION_PROMPTS
        
    def extract(self, text: str, doc_type: DocumentType, 
                target_fields: Optional[List[str]] = None) -> List[ExtractedField]:
        """Extract fields from document based on type"""
        
        # Get schema for document type
        schema = self._get_schema(doc_type)
        
        # Build extraction prompt
        prompt = self._build_extraction_prompt(doc_type, target_fields)
        
        # Create chain with output parser
        parser = PydanticOutputParser(pydantic_object=schema)
        
        extraction_prompt = PromptTemplate(
            template=prompt,
            input_variables=["text"],
            partial_variables={"format_instructions": parser.get_format_instructions()}
        )
        
        # Run extraction with multiple attempts for consistency
        extractions = []
        for _ in range(3):  # Multi-run for self-consistency
            try:
                chain = extraction_prompt | self.llm | parser
                result = chain.invoke({"text": text})
                extractions.append(result)
            except Exception as e:
                print(f"Extraction attempt failed: {e}")
                continue
        
        # Merge and score extractions
        fields = self._merge_extractions(extractions, doc_type)
        
        return fields
    
    def _get_schema(self, doc_type: DocumentType):
        """Get Pydantic schema for document type"""
        schemas = {
            DocumentType.INVOICE: InvoiceSchema,
            DocumentType.MEDICAL_BILL: MedicalBillSchema,
            DocumentType.PRESCRIPTION: PrescriptionSchema
        }
        return schemas.get(doc_type, InvoiceSchema)
    
    def _build_extraction_prompt(self, doc_type: DocumentType, 
                                target_fields: Optional[List[str]] = None) -> str:
        """Build extraction prompt with few-shot examples"""
        
        base_prompt = """You are an expert document extraction specialist.
        Extract the following information from the {doc_type} document.
        
        Document text:
        {text}
        
        {format_instructions}
        
        Additional requirements:
        1. Extract ALL relevant fields, even if not explicitly mentioned
        2. Infer values from context when direct extraction is not possible
        3. Maintain high accuracy and format consistency
        4. For monetary values, extract as numbers without currency symbols
        5. For dates, use ISO format (YYYY-MM-DD)
        """
        
        if target_fields:
            base_prompt += f"\n\nPrioritize extraction of these fields: {', '.join(target_fields)}"
        
        # Add few-shot examples based on document type
        examples = self.extraction_prompts.get(doc_type.value, {}).get('examples', '')
        if examples:
            base_prompt = examples + "\n\n" + base_prompt
        
        return base_prompt.replace("{doc_type}", doc_type.value)
    
    def _merge_extractions(self, extractions: List, doc_type: DocumentType) -> List[ExtractedField]:
        """Merge multiple extraction runs and calculate confidence"""
        
        if not extractions:
            return []
        
        # Count occurrences of each field value
        field_values = {}
        for extraction in extractions:
            if extraction:
                for field_name, field_value in extraction.dict().items():
                    if field_value is not None:
                        if field_name not in field_values:
                            field_values[field_name] = []
                        field_values[field_name].append(field_value)
        
        # Create ExtractedField objects with confidence
        extracted_fields = []
        for field_name, values in field_values.items():
            # Most common value (majority voting)
            most_common = max(set(values), key=values.count)
            
            # Calculate confidence based on agreement
            confidence = values.count(most_common) / len(values)
            
            extracted_fields.append(ExtractedField(
                name=field_name,
                value=most_common,
                confidence=confidence,
                validation_status="pending"
            ))
        
        return extracted_fields
```

## 2. **src/validators/field_validators.py**
```python
import re
from datetime import datetime
from typing import List, Dict, Any

from ..models.document_schemas import DocumentType, ValidationResult

class FieldValidator:
    """Validate extracted fields with various rules"""
    
    def __init__(self):
        self.validators = {
            'email': self._validate_email,
            'phone': self._validate_phone,
            'date': self._validate_date,
            'amount': self._validate_amount,
            'invoice_number': self._validate_invoice_number,
            'npi': self._validate_npi,
            'dea': self._validate_dea
        }
    
    def validate(self, fields: List[Dict], doc_type: DocumentType) -> ValidationResult:
        """Validate all fields and return results"""
        
        passed_rules = []
        failed_rules = []
        warnings = []
        
        # Field-level validation
        for field in fields:
            field_name = field['name'].lower()
            field_value = field['value']
            
            # Check field-specific validators
            for validator_key, validator_func in self.validators.items():
                if validator_key in field_name:
                    is_valid, message = validator_func(field_value)
                    if is_valid:
                        passed_rules.append(f"{field_name}_{validator_key}_valid")
                        field['validation_status'] = 'passed'
                    else:
                        failed_rules.append(f"{field_name}_{validator_key}_invalid: {message}")
                        field['validation_status'] = 'failed'
                        warnings.append(f"Field '{field_name}': {message}")
        
        # Cross-field validation
        cross_validation = self._validate_cross_fields(fields, doc_type)
        passed_rules.extend(cross_validation['passed'])
        failed_rules.extend(cross_validation['failed'])
        
        # Document-specific validation
        doc_validation = self._validate_document_specific(fields, doc_type)
        passed_rules.extend(doc_validation['passed'])
        failed_rules.extend(doc_validation['failed'])
        
        notes = f"{len(passed_rules)} rules passed, {len(failed_rules)} rules failed"
        if len([f for f in fields if f.get('confidence', 1.0) < 0.7]) >= 2:
            notes += f", {len([f for f in fields if f.get('confidence', 1.0) < 0.7])} low-confidence fields"
        
        return ValidationResult(
            passed_rules=passed_rules,
            failed_rules=failed_rules,
            notes=notes,
            warnings=warnings
        )
    
    def _validate_email(self, value: str) -> tuple:
        """Validate email format"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if re.match(pattern, str(value)):
            return True, "Valid email"
        return False, "Invalid email format"
    
    def _validate_phone(self, value: str) -> tuple:
        """Validate phone number format"""
        # Remove common separators
        cleaned = re.sub(r'[\s\-\(\)\.]+', '', str(value))
        if re.match(r'^\+?1?\d{10,14}$', cleaned):
            return True, "Valid phone"
        return False, "Invalid phone format"
    
    def _validate_date(self, value: Any) -> tuple:
        """Validate date format and reasonableness"""
        try:
            if isinstance(value, str):
                # Try multiple date formats
                for fmt in ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d']:
                    try:
                        date_obj = datetime.strptime(value, fmt)
                        # Check if date is reasonable (not too far in past or future)
                        if 1900 < date_obj.year < 2100:
                            return True, "Valid date"
                    except:
                        continue
            elif isinstance(value, datetime):
                if 1900 < value.year < 2100:
                    return True, "Valid date"
        except:
            pass
        return False, "Invalid or unreasonable date"
    
    def _validate_amount(self, value: Any) -> tuple:
        """Validate monetary amount"""
        try:
            # Convert to float, handling currency symbols and commas
            cleaned = re.sub(r'[$,]', '', str(value))
            amount = float(cleaned)
            if amount >= 0:
                return True, "Valid amount"
            return False, "Negative amount"
        except:
            return False, "Invalid amount format"
    
    def _validate_invoice_number(self, value: str) -> tuple:
        """Validate invoice number format"""
        # Common invoice patterns
        if re.match(r'^[A-Z0-9\-]{4,}$', str(value).upper()):
            return True, "Valid invoice number"
        return False, "Invalid invoice number format"
    
    def _validate_npi(self, value: str) -> tuple:
        """Validate NPI (National Provider Identifier)"""
        # NPI is 10 digits
        if re.match(r'^\d{10}$', str(value)):
            # Could add Luhn algorithm check here
            return True, "Valid NPI"
        return False, "Invalid NPI format"
    
    def _validate_dea(self, value: str) -> tuple:
        """Validate DEA number"""
        # DEA format: 2 letters + 7 digits
        if re.match(r'^[A-Z]{2}\d{7}$', str(value).upper()):
            return True, "Valid DEA"
        return False, "Invalid DEA format"
    
    def _validate_cross_fields(self, fields: List[Dict], doc_type: DocumentType) -> Dict:
        """Validate relationships between fields"""
        passed = []
        failed = []
        
        # Get field values by name
        field_dict = {f['name'].lower(): f['value'] for f in fields}
        
        # Check if totals match
        if 'total_amount' in field_dict and 'subtotal' in field_dict:
            try:
                total = float(field_dict['total_amount'])
                subtotal = float(field_dict['subtotal'])
                tax = float(field_dict.get('tax_amount', 0))
                
                calculated_total = subtotal + tax
                if abs(calculated_total - total) < 0.01:
                    passed.append("totals_match")
                else:
                    failed.append(f"totals_mismatch: calculated={calculated_total}, extracted={total}")
            except:
                failed.append("totals_validation_error")
        
        # Check date consistency
        if 'invoice_date' in field_dict and 'due_date' in field_dict:
            try:
                invoice_date = datetime.fromisoformat(str(field_dict['invoice_date']))
                due_date = datetime.fromisoformat(str(field_dict['due_date']))
                
                if due_date >= invoice_date:
                    passed.append("dates_consistent")
                else:
                    failed.append("due_date_before_invoice_date")
            except:
                pass
        
        return {'passed': passed, 'failed': failed}
    
    def _validate_document_specific(self, fields: List[Dict], doc_type: DocumentType) -> Dict:
        """Document type specific validation"""
        passed = []
        failed = []
        
        field_names = [f['name'].lower() for f in fields]
        
        if doc_type == DocumentType.INVOICE:
            required = ['invoice_number', 'total_amount', 'invoice_date']
            for req in required:
                if any(req in fn for fn in field_names):
                    passed.append(f"has_{req}")
                else:
                    failed.append(f"missing_{req}")
        
        elif doc_type == DocumentType.MEDICAL_BILL:
            required = ['patient_name', 'provider_name', 'charges']
            for req in required:
                if any(req in fn for fn in field_names):
                    passed.append(f"has_{req}")
                else:
                    failed.append(f"missing_{req}")
        
        elif doc_type == DocumentType.PRESCRIPTION:
            required = ['patient_name', 'medication_name', 'prescriber_name']
            for req in required:
                if any(req in fn for fn in field_names):
                    passed.append(f"has_{req}")
                else:
                    failed.append(f"missing_{req}")
        
        return {'passed': passed, 'failed': failed}
```

## 3. **src/agents/routing_agent.py**
```python
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from typing import Dict
import json

from ..models.document_schemas import DocumentType

class DocumentRouter:
    """Route documents to appropriate extraction pipelines"""
    
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.routing_prompt = self._create_routing_prompt()
    
    def _create_routing_prompt(self) -> ChatPromptTemplate:
        """Create the routing prompt template"""
        return ChatPromptTemplate.from_messages([
            ("system", """You are a document classification expert. 
            Analyze the document text and determine its type.
            
            Document types:
            1. INVOICE - Bills for goods/services with items, prices, totals
            2. MEDICAL_BILL - Healthcare charges with procedures, insurance info
            3. PRESCRIPTION - Medical prescriptions with medications and dosages
            
            Respond with ONLY the document type in JSON format.
            Example: {"type": "invoice", "confidence": 0.95}
            
            Key indicators:
            - Invoice: "Invoice #", "Bill To", "Total Amount", "Due Date", line items
            - Medical Bill: "Patient", "Provider", "CPT", "Diagnosis", "Insurance"
            - Prescription: "Rx", "Medication", "Dosage", "Prescriber", "DEA#"
            """),
            ("human", "Document text:\n{text}")
        ])
    
    def detect_type(self, text: str) -> DocumentType:
        """Detect document type from text"""
        
        # Truncate text if too long
        text_snippet = text[:3000] if len(text) > 3000 else text
        
        chain = self.routing_prompt | self.llm
        
        try:
            response = chain.invoke({"text": text_snippet})
            
            # Parse JSON response
            result = json.loads(response.content)
            doc_type = result.get('type', 'unknown').lower()
            
            # Map to DocumentType enum
            type_mapping = {
                'invoice': DocumentType.INVOICE,
                'medical_bill': DocumentType.MEDICAL_BILL,
                'prescription': DocumentType.PRESCRIPTION
            }
            
            return type_mapping.get(doc_type, DocumentType.UNKNOWN)
            
        except Exception as e:
            print(f"Routing error: {e}")
            # Fallback to keyword-based detection
            return self._fallback_detection(text)
    
    def _fallback_detection(self, text: str) -> DocumentType:
        """Fallback keyword-based detection"""
        text_lower = text.lower()
        
        invoice_keywords = ['invoice', 'bill to', 'total amount', 'subtotal', 'due date']
        medical_keywords = ['patient', 'provider', 'diagnosis', 'cpt', 'insurance']
        prescription_keywords = ['prescription', 'medication', 'dosage', 'refills', 'sig']
        
        invoice_score = sum(1 for kw in invoice_keywords if kw in text_lower)
        medical_score = sum(1 for kw in medical_keywords if kw in text_lower)
        prescription_score = sum(1 for kw in prescription_keywords if kw in text_lower)
        
        scores = {
            DocumentType.INVOICE: invoice_score,
            DocumentType.MEDICAL_BILL: medical_score,
            DocumentType.PRESCRIPTION: prescription_score
        }
        
        return max(scores, key=scores.get) if max(scores.values()) > 0 else DocumentType.UNKNOWN
```

## 4. **src/processors/ocr_processor.py**
```python
import pytesseract
from PIL import Image
import cv2
import numpy as np
from typing import Tuple, Dict, Any
import json

class OCRProcessor:
    """Process images using OCR"""
    
    def __init__(self):
        # Verify Tesseract installation
        try:
            pytesseract.get_tesseract_version()
        except:
            raise Exception("Tesseract not installed. Please install tesseract-ocr.")
    
    def process(self, image_path: str) -> Tuple[str, Dict[str, Any]]:
        """Process image and extract text with metadata"""
        
        # Preprocess image
        processed_image = self._preprocess_image(image_path)
        
        # Extract text with confidence scores
        data = pytesseract.image_to_data(processed_image, output_type=pytesseract.Output.DICT)
        
        # Extract full text
        text = pytesseract.image_to_string(processed_image)
        
        # Calculate metadata
        metadata = self._calculate_metadata(data)
        metadata['source'] = 'OCR'
        metadata['file_path'] = image_path
        
        return text, metadata
    
    def _preprocess_image(self, image_path: str) -> np.ndarray:
        """Preprocess image for better OCR results"""
        
        # Read image
        image = cv2.imread(image_path)
        
        # Convert to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        
        # Apply thresholding
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Denoise
        denoised = cv2.medianBlur(thresh, 3)
        
        # Deskew if needed
        angle = self._get_skew_angle(denoised)
        if abs(angle) > 0.5:
            denoised = self._rotate_image(denoised, angle)
        
        return denoised
    
    def _get_skew_angle(self, image: np.ndarray) -> float:
        """Detect skew angle of the image"""
        coords = np.column_stack(np.where(image > 0))
        if len(coords) > 0:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            return angle
        return 0
    
    def _rotate_image(self, image: np.ndarray, angle: float) -> np.ndarray:
        """Rotate image by given angle"""
        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(image, M, (w, h), 
                                flags=cv2.INTER_CUBIC,
                                borderMode=cv2.BORDER_REPLICATE)
        return rotated
    
    def _calculate_metadata(self, ocr_data: Dict) -> Dict[str, Any]:
        """Calculate OCR metadata"""
        
        confidences = [float(conf) for conf in ocr_data['conf'] if int(conf) > 0]
        
        metadata = {
            'total_words': len([word for word in ocr_data['text'] if word.strip()]),
            'avg_confidence': np.mean(confidences) if confidences else 0,
            'min_confidence': np.min(confidences) if confidences else 0,
            'max_confidence': np.max(confidences) if confidences else 0,
            'low_confidence_words': sum(1 for conf in confidences if conf < 60)
        }
        
        return metadata
```

## 5. **.env.example**
```bash
# OpenAI API Key (required)
OPENAI_API_KEY=sk-your-api-key-here

# Optional: Model configurations
OPENAI_MODEL=gpt-4-turbo-preview
TEMPERATURE=0.1
MAX_RETRIES=3

# Optional: Tesseract path (if not in system PATH)
# TESSERACT_CMD=/usr/local/bin/tesseract
```

## 6. **.gitignore**
```
# Environment variables
.env
.env.local

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# IDE
.vscode/
.idea/
*.sublime-*

# OS
.DS_Store
Thumbs.db

# Temporary files
*.tmp
*.temp
temp/
tmp/

# Data files (keep structure, ignore contents)
data/sample_documents/*
!data/sample_documents/.gitkeep
data/extraction_examples/*
!data/extraction_examples/.gitkeep

# Test coverage
htmlcov/
.coverage
.pytest_cache/

# Streamlit
.streamlit/secrets.toml
```
