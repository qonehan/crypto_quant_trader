● 변경 완료. 핵심 수정 내용은 다음과 같습니다.

  변경된 핵심 부분 (main() 함수, ~282행)                                                                                                                       
  
  기존 — 단일 엔진:                                                                                                                                            
  read_url = s.GCP_DB_URL or s.DB_URL                                                                                                                        
  engine = create_engine(read_url)                                                                                                                             
  features = load_features(engine, symbol)                                                                                                                   
  prices = load_prices(engine, symbol, ...)                                                                                                                    
                                                                                                                                                               
  수정 후 — 듀얼 엔진:                                                                                                                                         
  engine_local = create_engine(s.DB_URL)                                   # predictions (로컬)                                                                
  engine_gcp   = create_engine(s.GCP_DB_URL) if s.GCP_DB_URL else engine_local  # market_1s (GCP)                                                            
                                                                                                                                                               
  features = load_features(engine_local, symbol)   # predictions → 로컬 DB                                                                                     
  prices   = load_prices(engine_gcp, symbol, ...)  # market_1s   → GCP DB                                                                                      
                                                                                                                                                               
  - load_features() / load_prices() 함수 시그니처는 이미 engine을 인자로 받도록 되어 있어 내부 변경 없이 호출부만 수정했습니다.                                
  - GCP_DB_URL이 없으면 engine_gcp는 engine_local로 폴백되어 기존 단독 운영 환경과도 호환됩니다.  