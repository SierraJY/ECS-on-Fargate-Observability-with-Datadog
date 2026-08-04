### 1 개요

- Goal
    - 좌석 예약 도메인의 MSA를 ECS Fargate + Service Connect로 구축하고, Datadog으로 인프라·로그·APM·네트워크(VPC Flow Logs, Service Connect 메트릭)·프론트엔드(RUM)·DB(DBM)까지 통합 관측

---

### 2 서비스 구성

#### 2.1 토폴로지

- `Gateway` → `Reservation` →`{ Inventory, Payment } (병렬)` → `Notification`
- `Frontend`는 이 호출 체인에 포함되지 않음 — 브라우저가 ALB를 통해 Gateway를 직접 호출하는 정적 페이지, 서버사이드에서 다른 서비스를 호출하지 않음

#### 2.2 서비스별 상세

- **Gateway**
    - 역할: 외부 요청 진입점, Reservation으로 전달
    - 엔드포인트: `POST /reservations`, `GET /health`
- **Reservation**
    - 역할: Inventory(좌석 잠금)와 Payment(결제)를 병렬 호출. 둘 다 성공 시 Inventory 확정 처리 + Notification 호출. 하나라도 실패 시 Inventory 잠금 해제(보상 트랜잭션)
    - 엔드포인트: `POST /reservations`, `GET /health`
    - 호출 대상: Inventory, Payment, Notification (Service Connect FQDN, `<discoveryName>.jy-project`으로 접근 — 3.4.1 참고)
- **Inventory**
    - 역할: 좌석 재고 상태 관리, 동시 예약 충돌 방지, 유일하게 RDS 사용
    - DB: RDS PostgreSQL, `seats` 테이블 — `seat_id`, `status`(AVAILABLE/LOCKED/BOOKED), `locked_by`, `locked_at`
    - DBM: Datadog Agent 사이드카에 postgres 통합 설정 추가, RDS를 쿼리 성능·커넥션·락 레벨까지 관측(5.6 참고)
    - 엔드포인트:
        - `POST /seats/{seat_id}/lock` — AVAILABLE일 때만 LOCKED 전환(낙관적 락), 이미 잠긴 경우 409
        - `POST /seats/{seat_id}/confirm` — LOCKED → BOOKED
        - `POST /seats/{seat_id}/release` — LOCKED → AVAILABLE (결제 실패 시 롤백)
        - `GET /seats` — 전체 좌석 상태 조회
        - `GET /health`
- **Payment**
    - 역할: 결제 처리 mock, 외부 PG 호출 지연을 흉내냄
    - 엔드포인트: `POST /charge`, `GET /health`, `POST /admin/chaos` (장애 주입)
- **Notification**
    - 역할: 예약 확정 알림 mock (실제 발송 없이 로그만 남김)
    - 엔드포인트: `POST /notify`, `GET /health`
- **Frontend**
    - 역할: 좌석 예약 UI(정적 HTML/JS, nginx). 좌석 그리드 조회/예약/취소, 브라우저에서 Gateway를 직접 호출
    - Gateway/Reservation에 `GET /seats` 패스스루 추가하여 좌석 목록 노출(기존엔 Inventory 내부용으로만 존재)
    - Service Connect 미사용(서버사이드에서 다른 서비스 호출 없음), dd-trace 미적용(계측할 서버사이드 코드 없음, 대신 브라우저 관측은 RUM으로 수행 — 5.5 참고)

#### 2.3 공통 사항

- FastAPI로 구축
- `portMappings`에 `appProtocol: HTTP` 지정 (Service Connect L7 메트릭 활성화 조건)
- Gateway/Reservation/Inventory/Payment/Notification 5개 서비스는 Datadog Agent 사이드카 + FireLens 사이드카 + dd-trace 계측 동일 적용
- Frontend는 Datadog Agent + FireLens는 동일 적용하되 dd-trace는 제외(2.2 참고)

#### 2.4 API 상세 및 동작 흐름

- 전체 목적: 좌석을 예약하면 재고 확정(Inventory)과 결제(Payment)가 동시에 처리되고, 하나라도 실패하면 잠갔던 좌석을 다시 풀어주는(보상 트랜잭션) 흐름을 검증하는 좌석 예약 MSA 데모
- 외부에서 접근 가능한 진입점은 ALB → `Gateway` 하나뿐. `Reservation`/`Inventory`/`Payment`/`Notification`은 ALB에 연결되지 않고 Service Connect를 통한 내부 통신만 가능(3.2 ALB 참고)

**정상 흐름 (좌석 예약 성공)**

1. 클라이언트가 `Gateway`의 `POST /reservations`(body `{seat_id, user_id}`) 호출
2. `Gateway`는 받은 요청을 그대로 `Reservation`의 `POST /reservations`로 전달(pass-through, 응답도 그대로 반환)
3. `Reservation`은 `Inventory`의 `POST /seats/{seat_id}/lock`과 `Payment`의 `POST /charge`를 **동시에(병렬, asyncio.gather)** 호출
4. 둘 다 200이면 `Inventory`의 `POST /seats/{seat_id}/confirm` 호출 → 좌석 상태 `LOCKED` → `BOOKED`로 전환
5. confirm 성공 시 `Notification`의 `POST /notify` 호출(실제 발송 없이 로그만 남김)
6. `Gateway`가 최종 결과를 그대로 클라이언트에 반환: `{"status": "booked", "seat_id": ..., "user_id": ...}` (200)

**실패 흐름 (보상 트랜잭션)**

- `Inventory` lock 또는 `Payment` charge 중 하나라도 실패하면, 성공했던 쪽(주로 lock)을 `Inventory`의 `POST /seats/{seat_id}/release`로 되돌려 좌석을 `AVAILABLE` 상태로 복구
    - 실패 원인에 따라 상태 코드를 분리한다 — `Payment` 실패(다운스트림 서비스 장애, 리소스 충돌 아님)는 **502** 반환, `Inventory` lock만 실패(좌석이 이미 잠김/예약됨, 진짜 리소스 충돌)는 **409** 반환. 둘 다 실패하면 502 우선. 응답 바디는 동일하게 `{"detail": "reservation failed: inventory failed"}` 또는 `payment failed`(또는 둘 다)
    - 이 구분에 맞춰 APM span도 조정: 409(순수 좌석 충돌)는 non-error, 502(Payment 장애)는 ddtrace의 5xx 자동 마킹으로 error 처리 — 로그 파이프라인에서도 409는 error 대신 warning으로 재분류
- lock·charge는 둘 다 성공했으나 confirm이 실패하는 예외 케이스는 마찬가지로 release 호출 후 502 반환

**서비스별 API 상세**

| 서비스 | Method / Path | Request Body | 성공 응답 | 실패 조건 |
|---|---|---|---|---|
| Gateway | `GET /seats` | - | Reservation 응답을 그대로 전달(Frontend용 패스스루) | Reservation 응답 그대로 전달 |
| Gateway | `POST /reservations` | `{seat_id, user_id}` | Reservation 응답을 상태 코드 포함 그대로 전달 | Reservation 응답 그대로 전달 |
| Gateway | `POST /reservations/{seat_id}/cancel` | `{user_id}` | Reservation 응답을 상태 코드 포함 그대로 전달 | Reservation 응답 그대로 전달 |
| Gateway | `POST /admin/chaos` | `{mode, delay_ms, error_rate}` | Payment 응답을 그대로 전달(Payment가 ALB 미연결이라 프록시 필요) | Payment 응답 그대로 전달 |
| Gateway | `GET /health` | - | `{"status":"ok"}` | - |
| Reservation | `POST /reservations` | `{seat_id, user_id}` | `{"status":"booked", seat_id, user_id}` (200) | Inventory lock 실패 시 409(순수 충돌), Payment 실패 또는 confirm 실패 시 502(다운스트림 장애) |
| Reservation | `POST /reservations/{seat_id}/cancel` | `{user_id}` | `{"status":"cancelled", seat_id, user_id}` (200) | 좌석이 BOOKED 상태가 아니면 409 |
| Reservation | `GET /health` | - | `{"status":"ok"}` | - |
| Inventory | `POST /seats/{seat_id}/lock` | `{locked_by}` | `{seat_id, status:"LOCKED"}` (200) | 이미 잠김/BOOKED이거나 존재하지 않는 좌석 → 409 |
| Inventory | `POST /seats/{seat_id}/confirm` | - | `{seat_id, status:"BOOKED"}` (200) | LOCKED 상태가 아니면 409 |
| Inventory | `POST /seats/{seat_id}/release` | - | `{seat_id, status:"AVAILABLE"}` (200) | LOCKED 상태가 아니면 409 |
| Inventory | `POST /seats/{seat_id}/cancel` | - | `{seat_id, status:"AVAILABLE"}` (200) | BOOKED 상태가 아니면 409 |
| Inventory | `GET /seats` | - | 전체 좌석 배열 `[{seat_id, status, locked_by, locked_at}, ...]` | - |
| Inventory | `GET /health` | - | `{"status":"ok"}` (DB 연결 확인 포함, 실패 시 503) | - |
| Payment | `POST /charge` | `{user_id, amount}` | `{"status":"charged", user_id, amount}` (200, mock — 기본은 항상 성공) | `/admin/chaos`로 장애 주입 시 500(error 모드) |
| Payment | `POST /admin/chaos` | `{mode, delay_ms, error_rate}` | 현재 chaos 설정 그대로 반환(200) | - (인증 없음, 데모 전용 — 7.4 참고) |
| Payment | `GET /health` | - | `{"status":"ok"}` | - |
| Notification | `POST /notify` | `{user_id, message}` | `{"status":"sent", user_id}` (200, 로그만 남기고 실제 발송 없음) | - |
| Notification | `GET /health` | - | `{"status":"ok"}` | - |

- `Inventory`는 앱 기동 시 `seats` 테이블이 없으면 자동 생성하고, 좌석 시드는 **멱등 리컨실 방식**을 사용한다 — 매 기동 시 목표 seat_id 집합을 `INSERT ... ON CONFLICT DO NOTHING`으로 채우고, 목표 집합에 없는 옛 seat_id는 `DELETE`로 정리. 재배포만 하면 항상 최신 좌석 구성으로 수렴함
- 좌석 구성: 영화관 좌석도(행 A~F, 3구역, 사다리꼴, 150석). 행별 구성(앞줄일수록 좁고 뒷줄일수록 넓은 사다리꼴): A행 20석(6~9, 10~21, 22~25), B행 22석, C행 24석, D행 26석, E행 28석, F행 30석(1~9, 10~21, 22~30) — 중앙 구역(10~21, 12석)은 모든 행 공통, 좌/우 구역만 행마다 확장

---

### 3. 인프라 구성

#### 3.1 VPC & 네트워크

- [VPC]
    - `jy-project-vpc(vpc-09a226651277446d9)`
    - CIDR: 10.0.0.0/24
- [Subnet]
    - Public
        - `jy-project-vpc-public-subnet-2a`
            - 10.0.0.0/26
            - ALB, NAT Gateway
        - `jy-project-vpc-public-subnet-2c`
            - 10.0.0.64/26
            - ALB
    - Private
        - `jy-project-vpc-private-subnet-2a`
            - 10.0.0.128/26
            - ECS Task, RDS
        - `jy-project-vpc-private-subnet-2c`
            - 10.0.0.192/26
            - ECS Task, RDS
- [Internet Gateway]
    - `jy-project-vpc-igw`
- [NAT Gateway]
    - `jy-project-vpc-nat-gw`
        - 배치: `jy-project-vpc-public-subnet-2a`
        - EIP 할당
- [Routing Table]
    - `jy-project-vpc-public-rt`
        - Destination: 0.0.0.0/0 -> `jy-project-vpc-igw`
        - Subnet associations: `jy-project-vpc-public-subnet-2a`, `jy-project-vpc-public-subnet-2c`
    - `jy-project-vpc-private-rt`
        - Destination: 0.0.0.0/0 -> `jy-project-vpc-nat-gw`
        - Subnet associations: `jy-project-vpc-private-subnet-2a`, `jy-project-vpc-private-subnet-2c`
- [Security Group]
    - `jy-project-alb-sg`
        - ALB SG: 80 인바운드 전체 허용, ECS Task SG로 아웃바운드
        - Inbound rules
            - Type HTTP(80), Source Anywhere-IPv4(0.0.0.0/0)
        - Outbound rules
            - 기본값
    - `jy-project-ecs-task-sg`
        - ECS Task SG: ALB SG로부터 Gateway 포트 인바운드, 같은 SG 내 서비스 간 인바운드(Service Connect), RDS SG·인터넷(NAT)으로 아웃바운드
        - Inbound rules
            - Type Custom TCP, Port 8000, Source Custom → jy-project-alb-sg 선택
            - 생성 완료 후, 이 SG를 다시 열어서Inbound rules 1개 추가
                - Type Custom TCP, Port 8000, Source Custom → 자기 자신(jy-project-ecs-task-sg) 선택 후 저장
                - Service Connect로 서비스끼리 통신할 때 필요한 규칙
            - Type Custom TCP, Port 80, Source Custom → `jy-project-alb-sg` — Frontend 컨테이너(nginx) 인바운드용
        - Outbound rules
            - 기본값
    - `jy-project-rds-sg`
        - RDS SG: ECS Task SG로부터만 5432 인바운드
        - Inbound rules
            - PostgreSQL(자동으로 포트 5432 지정), Source Custom → `jy-project-ecs-task-sg`
        - Outbound rules
            - 기본값

#### 3.2 ALB

- [Target Group]
    - `jy-project-gateway-tg`
        - Target type: **IP addresses** (awsvpc 모드라 반드시)
        - Protocol: HTTP, Port: 8000 (Gateway 컨테이너 포트와 동일)
        - VPC: `jy-project-vpc`
        - Health checks: Health check path `/health`, interval **180초**
    - `jy-project-frontend-tg`
        - Target type: IP addresses, Protocol: HTTP, Port: 80 (nginx 컨테이너 포트)
        - VPC: `jy-project-vpc`
        - Health checks: Health check path `/`, interval **180초**
- [ALB]
    - `jy-project-alb`
        - Scheme: Internet-facing
        - IP address type: IPv4
        - VPC: `jy-project-vpc`
        - Mappings: **Public** 서브넷 2개(2a, 2c)
        - Security groups: `jy-project-alb-sg`
        - Listeners and routing: Protocol HTTP, Port 80
            - Rule(Priority 1): path-pattern `/reservations*`, `/seats`, `/health` → `jy-project-gateway-tg`
            - Default action: `jy-project-frontend-tg`
        - Reservation/Inventory/Payment/Notification은 ALB 미연결 — Service Connect 내부 통신만
        - Frontend는 ALB에 연결되지만 Service Connect는 미사용(2.2 참고)

#### 3.3 RDS (Inventory 전용)

- DB Subnet Group
    - `jy-project-rds-subnet-group`
        - VPC: `jy-project-vpc`
        - Add subnets: AZ `ap-northeast-2a`, `ap-northeast-2c` Private 서브넷
- [RDS]
    - DB instance identifier: **`jy-project-db`**
    - Availabil`ity and durability: Single DB instance`
    - Credentials managemen`t: Managed in AWS Secrets Manager`
    - Instance configurati`on: db.t3.micro`
    - Storage
        - gp3
        - 20GB
        - Storage autoscaling 끔
    - Connectivity
        - VPC: `jy-project-vpc`
        - DB subnet group: `jy-project-rds-subnet-group`
        - Public access: No
        - VPC security group: `jy-project-rds-sg`
- 자격증명: RDS 콘솔의 Secrets Manager 관리형 옵션 사용

#### 3.4 ECS / Service Connect

- [Name Space]
    - Cloud Map 네임스페이스
    - Namespace name: `jy-project`
    - Namespace type: API 호출
    - 서비스별 discoveryName: `reservation`, `inventory`, `payment`, `notification` (Gateway는 서버 역할 없어 discoveryName 없음 — 3.4.1 참고)
- [Fargate Cluster]
    - `jy-project-cluster`
        - Infrastructure: AWS Fargate (serverless)
        - **Namespace :**`jy-project`

#### 3.4.1 ECS 서비스 생성 시 주의사항

- [서비스 이름]
    - 콘솔 기본 제안값(`jy-project-<service>-service-<랜덤문자열>`) 그대로 두지 않음 — CI 워크플로우가 찾는 이름과 불일치해 `Deploy to ECS` 실패
    - ECS 서비스는 생성 후 이름 변경 불가, 삭제 후 재생성 필요
    - 확정 규칙: `jy-project-<service>-service` (랜덤 접미사 없이 직접 입력)
- [네트워크]
    - 서브넷: private(2a, 2c) 2개만 선택
    - 퍼블릭 IP 자동 할당: 비활성화 (private 서브넷은 IGW 라우트 없어 실효 없음, NAT Gateway 아웃바운드 구조와 불일치)
- [Service Connect 모드]
    - 기준: 다른 서비스가 Service Connect로 호출해야 하는가

    | 서비스 | 모드 | 이유 |
    |---|---|---|
    | Gateway | 클라이언트 모드 | Reservation만 호출, ALB가 태스크 IP 직접 타겟팅이라 Gateway를 부르는 서비스 없음 |
    | Reservation | 클라이언트-서버 모드 | Gateway가 호출(서버) + Inventory/Payment/Notification 호출(클라이언트) |
    | Inventory / Payment / Notification | 클라이언트-서버 모드 | Reservation이 호출 |
    | Frontend | **미사용** | 서버사이드에서 다른 서비스를 호출하지 않음(브라우저가 직접 ALB 호출) — Service Connect 자체를 켤 필요 없음 |

    - 클라이언트 모드도 Service Connect 자체(네임스페이스 `jy-project`)는 켜져 있어야 함 — 짧은 이름은 Envoy 사이드카 기반 특수 주소라 호출하는 쪽도 꺼져 있으면 통신 불가
    - 클라이언트-서버 모드: 포트 매핑(`inventory-8000` 등) 추가 + Discovery name을 서비스명 그대로(`inventory` 등) 직접 지정
- [Service Connect DNS]
    - discovery name이 `inventory`여도 실제 resolve 가능한 주소는 `inventory.jy-project`(네임스페이스 접미사 포함)
    - taskdef 환경변수(`INVENTORY_URL` 등)는 FQDN으로 설정 — 짧은 이름은 `Name or service not known`
    - 로컬 `docker-compose.local.yml`은 도커 임베디드 DNS라 영향 없음
- [Execution Role 권한]
    - `AmazonECSTaskExecutionRolePolicy`는 `logs:CreateLogGroup` 미포함
    - `awslogs-create-group: true` 사용 시 이 권한 없으면 `ResourceInitializationError`로 태스크 기동 자체 실패
    - `ecs-demo-execution-role`에 `logs:CreateLogGroup`(리소스: `arn:aws:logs:ap-northeast-2:263232886346:log-group:/ecs/jy-project-*`) 인라인 정책 추가로 해결
- [컨테이너 헬스체크]
    - Gateway를 제외한 4개 서비스(Reservation/Inventory/Payment/Notification)는 ALB에 안 붙어 있어 헬스체크 자체가 없었고, taskdef에도 컨테이너 `healthCheck`가 없어서 ECS가 "프로세스가 살아있는지"만 보고 "앱이 실제로 정상 응답하는지"는 전혀 모르는 상태였음(Service Connect도 ECS가 아는 것 이상은 모름 — 같은 공백)
    - 5개 서비스 앱 컨테이너 전부에 `healthCheck` 추가: `curl -f http://localhost:8000/health || exit 1`, `interval: 180`(헬스체크가 너무 잦다는 판단하에 완화한 값), `timeout: 5`, `retries: 3`, `startPeriod: 10`
    - curl이 없던 4개 서비스(gateway/reservation/payment/notification) Dockerfile에 curl 설치 단계 추가(inventory는 RDS CA 번들 다운로드 때문에 이미 있었음)
    - UNHEALTHY 판정되면 ECS가 태스크를 자동 교체하고, Service Connect도 그 태스크로는 라우팅을 멈춤 — desired count가 1이라 지금은 효과가 제한적이지만 replica를 늘리면 의미가 커짐

#### 3.5 ECR

- 리포지토리 6개(서비스당 1개, frontend 포함)
    - `jy-project/gateway`
    - `jy-project/reservation`
    - `jy-project/inventory`
    - `jy-project/payment`
    - `jy-project/notification`
    - `jy-project/frontend`

### 3.6 IAM Role

- Role name: `ecs-demo-execution-role`
    - Trusted entity type: `AWS service`
    - Use case: `Elastic Container Service` → **`Elastic Container Service Task`**
    - Permissions policies: `AmazonECSTaskExecutionRolePolicy`
        - ECR pull + CloudWatch Logs 쓰기 권한 포함된 AWS 관리형 정책
    - 생성 후 Secrets Manager 읽기 권한을 추가 (인라인 정책명: `secrets-read`)
        - RDS 자격증명을 `DATABASE_URL`로 주입하려면 필요
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "secretsmanager:GetSecretValue",
                  "Resource": "arn:aws:secretsmanager:ap-northeast-2:263232886346:secret:*"
                }
              ]
            }
            ```
            
    - `logs:CreateLogGroup` 인라인 정책 추가 (정책명: `allow-cloudwatch-log-group-creation`)
        - `AmazonECSTaskExecutionRolePolicy`가 `CreateLogStream`/`PutLogEvents`만 포함하고 `CreateLogGroup`은 빠져 있어서, taskdef의 `awslogs-create-group: true`가 동작하려면 별도 추가 필요 (3.4.1 참고)
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "logs:CreateLogGroup",
                  "Resource": "arn:aws:logs:ap-northeast-2:263232886346:log-group:/ecs/jy-project-*"
                }
              ]
            }
            ```
            
    - SSM Parameter Store 읽기 + KMS 복호화 권한 추가 (인라인 정책명: `param_read`)
        - Datadog Agent/FireLens 컨테이너가 `/jy-project/*` 아래 SSM 파라미터(Datadog API Key 등, 3.7 참고)를 Fargate 기동 시점에 직접 조회하려면 필요. `kms:Decrypt`가 없으면 `datadog-api-key`(SecureString) 조회 시 `ResourceInitializationError`로 태스크 기동 실패
            
            ```jsx
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": "ssm:GetParameters",
                  "Resource": "arn:aws:ssm:ap-northeast-2:263232886346:parameter/jy-project/*"
                },
                {
                  "Effect": "Allow",
                  "Action": "kms:Decrypt",
                  "Resource": "arn:aws:kms:ap-northeast-2:263232886346:key/cc5ac505-a5e1-49ba-b1c3-1093adc44a74"
                }
              ]
            }
            ```
            
- Role name: ecs-demo-task-role
    - Trusted entity type: `AWS service`
    - Use case: `Elastic Container Service` → **`Elastic Container Service Task`**
    - Permissions policies:
    - 나중에 Datadog Agent 붙일 때 필요한 권한을 여기에 추가 — Agent가 ECS/CloudWatch API를 폴링하며 `AccessDenied`를 내는 경우에만 최소 권한으로 추가 예정(현재 미정)

### 3.7 SSM Parameter Store

- Datadog 사이드카 설정을 위해 taskdef 5개 파일에 흩어져 있던 ARN과 Datadog 설정값을 `/jy-project/*` 네임스페이스로 중앙 관리
- `.env` 파일 방식은 검토 후 채택하지 않음 — CI 러너가 gitignore된 로컬 파일을 읽을 수 없어 결국 GitHub Actions Variables 같은 별도 CI 전용 저장소가 필요해지고, 로컬/CI 두 저장소를 사람이 수동 동기화해야 하는 drift 위험이 있어 SSM 단일 소스로 통일
- 소비 방식 두 갈래: taskdef 최상위 필드·FireLens 옵션처럼 등록 시점에 값이 확정돼야 하는 항목은 CI가 조회 후 치환, 컨테이너 `secrets.valueFrom`으로 넣을 수 있는 항목은 ECS가 Fargate 기동 시점에 직접 조회(3.6의 `param_read`, 4.4의 OIDC 역할 권한이 각각 이 두 갈래에 대응)

| Path | Type | 값 | 용도 |
|---|---|---|---|
| `/jy-project/ecs-execution-role-arn` | String | `arn:aws:iam::263232886346:role/ecs-demo-execution-role` | taskdef 최상위 `executionRoleArn`, CI가 등록 전 치환 |
| `/jy-project/ecs-task-role-arn` | String | `arn:aws:iam::263232886346:role/ecs-demo-task-role` | taskdef 최상위 `taskRoleArn`, CI가 등록 전 치환 |
| `/jy-project/rds-secret-arn` | String | `arn:aws:secretsmanager:ap-northeast-2:263232886346:secret:rds!db-18dce600-05b3-4a42-9284-3eeb5b617745-NHTBAD` | inventory taskdef의 RDS `secrets.valueFrom` 접두사, CI가 등록 전 치환. 값 자체는 3.3의 RDS 관리형 Secrets Manager 시크릿 ARN을 그대로 가리키는 포인터 |
| `/jy-project/dd-site` | String | `datadoghq.com` | Datadog Agent 컨테이너는 `secrets.valueFrom`으로 런타임에 직접 조회, FireLens `Host` 옵션 문자열은 CI가 치환 |
| `/jy-project/inventory-db-host` | String | `jy-project-db.cxsmy4yg60ts.ap-northeast-2.rds.amazonaws.com` | inventory 컨테이너와 datadog-agent(DBM)가 `secrets.valueFrom`으로 런타임에 직접 조회(RDS가 private 서브넷·퍼블릭 액세스 차단이라 보안상 필수는 아니지만, RDS 재생성 시 taskdef 직접 수정을 피하려고 중앙화). CI 치환 불필요 — SSM 파라미터 ARN이 계정ID+리전+이름으로 결정되는 값이라 taskdef에 고정 문자열로 기입 |

- `/jy-project/datadog-api-key`(SecureString)는 더 이상 사용하지 않음 — Secrets Manager로 이전(3.7-1 참고)

#### 3.7-1 Secrets Manager

- 이유: VPC Flow Logs/Service Connect Firehose destination 설정 화면(콘솔)이 API 키 값을 "Secrets Manager에서 직접 조회" 옵션만 지원하고 SSM Parameter Store는 지원하지 않음 — 매번 콘솔에서 API 키를 수동 복붙해야 하는 번거로움을 피하기 위해 채택
- RDS 마스터 계정(3.3)처럼 자동 로테이션이 필요해서가 아니라, 순수하게 콘솔 연동 편의 때문에 채택한 예외적인 케이스(5.1에서 SSM을 기본값으로 삼은 이유와 대비)

| Secret name | 저장 형식 | 값 | 용도 |
|---|---|---|---|
| `jy-project/datadog-api-key` | Plaintext (단일 문자열) | Datadog API Key 원문 | 6개 서비스 taskdef의 `DD_API_KEY`(Agent)/`apikey`(FireLens) `secrets.valueFrom`이 참조. Firehose destination 콘솔 설정 시에도 재사용 |
| `jy-project/inventory-dbm-datadog` | Key/value (`username`, `password`) | DBM 전용 읽기 전용 DB 유저(`pg_monitor` 부여) 자격증명 | Inventory datadog-agent 컨테이너의 `DD_POSTGRES_USER`/`DD_POSTGRES_PASSWORD` `secrets.valueFrom`이 `:username::`/`:password::` 접미사로 참조 |

- IAM: 기존 `ecs-demo-execution-role`의 `secrets-read` 인라인 정책이 `secret:*` 와일드카드라 별도 권한 추가 불필요(3.6 참고)

---

## 4. CI/CD

#### 4.1 저장소 구조

- 모노레포, 서비스별 디렉터리 분리

```
/gateway, /reservation, /inventory, /payment, /notification
  각 디렉터리: Dockerfile, main.py, requirements.txt
/frontend
  Dockerfile, index.html, app.js, style.css, config.js
/scripts
  load-test.sh, reset-seats.sh, chaos.sh
/.github/workflows/build-and-deploy.yml
```

#### 4.2 파이프라인 범위

- 최초 인프라(클러스터·서비스·VPC·RDS)는 콘솔에서 수동 생성. Task Definition은 최초 push부터 CI가 자동 등록(수동 생성 아님). 이후 코드 변경 시 이미지 빌드부터 서비스 배포까지는 CI가 자동화

#### 4.3 워크플로우 단계

1. 코드 체크아웃
2. AWS 인증 (OIDC로 IAM Role 위임, Access Key 미사용)
3. ECR 로그인
4. 서비스별 Docker 이미지 빌드 (matrix 전략, 워크플로우 파일 1개로 5개 서비스 처리)
5. ECR 푸시 — 커밋 SHA 태그만 사용 (`latest` 태그 미사용)
6. Task Definition JSON 템플릿(레포에 커밋된 5개 파일)의 이미지 URI를 새 이미지로 교체 (`amazon-ecs-render-task-definition` 액션)
7. `register-task-definition`으로 새 리비전 등록, `update-service`로 서비스 갱신 (`amazon-ecs-deploy-task-definition` 액션)

트리거: `main` 브랜치 push

#### 4.4 IAM Role 권한 (OIDC)

- Role: `arn:aws:iam::263232886346:role/github-actions-ecs-deploy`
    - GitHub Actions(OIDC)가 위임받아 사용하는 역할. ECS-on-Fargate-Observability-with-Datadog 레포의 CI/CD 파이프라인에서 ECR 이미지 풀/푸시, ECS Task Definition 등록, 서비스 배포에 사용됨
    - Inline Policy
        
        ```jsx
        {
        	"Version": "2012-10-17",
        	"Statement": [
        		{
        			"Effect": "Allow",
        			"Action": "ecr:GetAuthorizationToken",
        			"Resource": "*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecr:BatchCheckLayerAvailability",
        				"ecr:GetDownloadUrlForLayer",
        				"ecr:BatchGetImage",
        				"ecr:PutImage",
        				"ecr:InitiateLayerUpload",
        				"ecr:UploadLayerPart",
        				"ecr:CompleteLayerUpload"
        			],
        			"Resource": "arn:aws:ecr:ap-northeast-2:263232886346:repository/*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecs:RegisterTaskDefinition",
        				"ecs:DescribeTaskDefinition"
        			],
        			"Resource": "*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ecs:UpdateService",
        				"ecs:DescribeServices"
        			],
        			"Resource": "arn:aws:ecs:ap-northeast-2:263232886346:service/*"
        		},
        		{
        			"Effect": "Allow",
        			"Action": "iam:PassRole",
        			"Resource": [
        				"arn:aws:iam::263232886346:role/ecs-demo-task-role",
        				"arn:aws:iam::263232886346:role/ecs-demo-execution-role"
        			],
        			"Condition": {
        				"StringEquals": {
        					"iam:PassedToService": "ecs-tasks.amazonaws.com"
        				}
        			}
        		},
        		{
        			"Effect": "Allow",
        			"Action": [
        				"ssm:GetParameters",
        				"ssm:GetParametersByPath"
        			],
        			"Resource": "arn:aws:ssm:ap-northeast-2:263232886346:parameter/jy-project/*"
        		}
        	]
        }
        ```
        
    - taskdef 등록 전 SSM 값 치환(3.7 참고)을 위해 `ssm:GetParameters`/`ssm:GetParametersByPath` 추가. **의도적으로 `kms:Decrypt`는 부여하지 않음** — `datadog-api-key`(SecureString)를 이 역할이 복호화하지 못하게 막아, 원문 API Key가 CI 파이프라인 경로를 절대 거치지 않도록 하는 방어선

---

## 5. 관측성 통합 체크리스트

- 5개 서비스(Gateway/Reservation/Inventory/Payment/Notification) 전부 동일 적용 (Notification 포함 — 하나라도 빠지면 5.3 Trace-to-Log 체인이 끊김)
- Frontend는 5.1/5.2는 동일 적용, 5.3(dd-trace)만 제외(2.2 참고)

#### 5.1 Datadog Agent 사이드카 (인프라 메트릭)

- 컨테이너명 `datadog-agent`, 이미지 `public.ecr.aws/datadog/agent:latest`(ECR Public, Docker Hub rate limit 회피 — 5.2의 FireLens 이미지 선택과 동일한 이유)
- `cpu: 256`, `memory: 512`(6번 리소스 스펙), `essential: false`(Agent 장애가 앱까지 죽이지 않도록)
- 환경변수: `ECS_FARGATE=true`, `DD_APM_ENABLED=true`(5.3 dd-trace용)
- `DD_SITE`는 SSM Parameter Store에서 `secrets.valueFrom`으로 Fargate 기동 시점에 직접 조회(3.7 참고) — RDS 자격증명과 달리 Datadog 쪽은 자동 로테이션이 필요 없어 무료 티어인 SSM이 기본값
- `DD_API_KEY`는 Secrets Manager에서 `secrets.valueFrom`으로 조회(SSM이 아닌 예외적 케이스, 이유는 3.7-1 참고)

#### 5.2 FireLens 사이드카 (로그)

- 컨테이너명 `log-router`, 이미지 `public.ecr.aws/aws-observability/aws-for-fluent-bit:stable`(ECR Public, Docker Hub rate limit 회피)
- `firelensConfiguration.type: fluentbit`, `options.enable-ecs-log-metadata: true`(로그에 ECS 클러스터/태스크/컨테이너 메타데이터 자동 태깅)
- 앱 컨테이너 `logConfiguration.logDriver: awsfirelens`로 전환, `dependsOn`으로 `log-router` `START` 이후 기동하도록 지정(초기 로그 유실 방지)
- Datadog 목적지 옵션: `Name: datadog`, `Host: http-intake.logs.${DD_SITE}`(CI가 SSM `dd-site` 값으로 등록 전 치환), `dd_service`를 서비스명으로 지정, `dd_tags: env:dev`
- `secretOptions.apikey`는 SSM Parameter Store(`datadog-api-key`, SecureString)를 ECS가 Fargate 기동 시점에 직접 조회
- `log-router` 자체의 프로세스 로그는 Datadog이 아니라 별도 CloudWatch 로그 그룹(`/ecs/jy-project-<service>-firelens`)으로 분리 — Fluent Bit 장애 시 디버깅 경로를 앱 로그 파이프라인과 독립시키기 위함
- 이 전환 이후 앱 로그는 CloudWatch로 더 이상 가지 않고 Datadog으로만 전송됨(과거 CloudWatch 로그 그룹은 조회용으로 남겨둠, 정리 대상 아님)

#### 5.3 dd-trace (APM)

- `ddtrace==2.14.4` 라이브러리 설치(5개 서비스 requirements.txt), 기동 명령 `ddtrace-run uvicorn main:app`(5개 서비스 Dockerfile CMD)
- 환경변수(Unified Service Tagging): `DD_SERVICE=<service명>`, `DD_ENV=dev`, `DD_VERSION=0.0.1`, `DD_AGENT_HOST=localhost`, `DD_TRACE_AGENT_PORT=8126`(기본값과 동일하지만 암묵적 의존 대신 명시)
- Datadog Agent 컨테이너의 `DD_APM_ENABLED`는 `true`로 설정(APM 활성화)
- 서비스 간 호출(httpx)의 트레이스 컨텍스트는 자동 전파됨 — Reservation→Inventory/Payment→Notification 체인이 트레이스 하나로 엮임(Datadog APM Trace 화면에서 확인 가능)
- **커스텀 span 태그(Reservation)**: 장애/보상 트랜잭션 진단용으로 `seat_id`, `usr.id`(user_id)를 모든 요청에, 실패 시 `failure.stage`(`lock`/`charge`/`confirm`/`cancel`)와 `failure.reason`(`inventory_failed`/`payment_failed`/`confirm_failed`/`not_booked`)을 추가 태깅. ddtrace가 기본적으로 5xx만 에러로 자동 마킹하는 점을 보완하기 위해 409/502 실패 지점에서 `span.error = 1`을 명시적으로 설정

#### 5.4 네트워크

- CNM은 Preview·별도 신청 필요로 채택 불가 확정 → self-service 대안으로 아래 두 가지 구현(README 4.2 참고)
- **VPC Flow Logs**: VPC Flow Logs → Kinesis Data Firehose(직접) → Datadog. 비용 절감을 위해 리소스는 비활성화 상태로 유지(상시 운영 안 함, 필요 시 재활성화)
- **Service Connect 메트릭**: `AWS/ECS` 네임스페이스 CloudWatch 네이티브 메트릭을 CloudWatch Metric Streams(Firehose, OpenTelemetry 1.0 포맷)로 Datadog에 연동 — 상시 운영
- **Service Connect Access Log (Envoy)**: 5개 서비스(gateway/reservation/inventory/payment/notification) 전부 `serviceConnectConfiguration`에 `logConfiguration`(`logDriver: awsfirelens`)과 `accessLogConfiguration`(`format: JSON`, `includeQueryParameters: DISABLED`)을 지정. 로그 드라이버는 각 서비스 taskdef에 이미 떠 있는 FireLens `log-router` 사이드카를 그대로 재사용(별도 컨테이너 추가 없음), Datadog 출력 옵션(`Name: datadog`, `Host: http-intake.logs.datadoghq.com`, `TLS: on`, `provider: ecs`)과 API Key(`secretOptions.apikey`, 3.7-1의 Secrets Manager 시크릿 재사용)도 앱 컨테이너 FireLens 설정과 동일한 값을 씀. 앱 로그와 구분하기 위해 `dd_service: <service>-envoy`, `dd_source: envoy` 태그 사용
- **알려진 한계**: client-only 모드(discoveryName 없이 다른 서비스만 호출하는 서비스, 예: Gateway)는 자신이 발신하는 요청의 access log가 찍히지 않는 것으로 관찰됨 — 단 해당 요청은 상대 서비스(수신 측)의 access log로는 잡히므로 체인 전체가 관측에서 비는 건 아님. 원인은 AWS 공식 문서에 명시돼 있지 않아 추정 단계

#### 5.5 Frontend + RUM

- Frontend 서비스(정적 페이지, nginx)는 ALB 경로 기반 라우팅(`/reservations*`, `/seats`, `/health` → Gateway, 나머지 default → Frontend)으로 서빙됨
- **Datadog RUM(Browser) 연동**: CDN Sync 방식(`datadoghq-browser-agent.com` 스크립트를 `index.html` head에서 동기 로드) 채택 — 이 프로젝트가 빌드 파이프라인 없는 정적 파일 구조(2.2 참고)라 npm 패키지 대신 CDN 방식이 일관됨
    - `service: frontend`, `env: dev`, `version: 0.0.1`(백엔드 서비스들의 Unified Service Tagging 컨벤션과 통일, 5.3 참고)
    - `allowedTracingUrls`는 `config.js`의 기존 `isLocal` 판별(`apiBase`) 값을 재사용 — 로컬(포트 분리)/ECS(같은 오리진) 환경 모두에서 ALB 도메인을 하드코딩하지 않고 실제 API 요청 URL과 자동 매칭되도록 구성
    - dd-trace(Gateway APM)와 RUM 트레이스가 연결되어 Browser → Gateway 체인이 하나의 트레이스로 확인됨

#### 5.6 DBM (Database Monitoring)

- RDS PostgreSQL 18은 `shared_preload_libraries`에 `pg_stat_statements`가 기본값으로 이미 preload되어 있어 파라미터 그룹 변경/재부팅이 불필요하다. Performance Insights는 미활성화 상태 — 채택 방식(Agent가 DB에 직접 SQL 접속)에서는 필수 요건이 아님
- `inventory` DB에 `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` 실행, 전용 읽기 전용 유저 `datadog` 생성 후 `pg_monitor` 역할 부여(3.7-1의 Secrets Manager 자격증명)
- 수집 방식: Inventory에 이미 떠 있는 `datadog-agent` 사이드카 재사용(별도 Agent 불필요). RDS는 같은 태스크의 컨테이너가 아니라 외부 서비스라 Docker Autodiscovery 대상 컨테이너가 없음 — **Agent 컨테이너 자신에게 `dockerLabels.com.datadoghq.ad.checks` Autodiscovery 라벨을 붙이는 방식**으로 postgres 통합 설정(Datadog 공식 RDS DBM 가이드의 패턴), `%%env_VARNAME%%` 템플릿 변수로 시크릿을 라벨 문자열에 직접 노출하지 않음
- `sslmode: require` 채택 — RDS 파라미터 `rds.force_ssl=1`은 암호화만 강제하고 CA 검증까지 강제하지 않아서, 앱(inventory)이 쓰는 `verify-full`과 달리 Agent 쪽엔 CA 번들을 넣지 않아도 됨
- Databases Explorer에서 실제 쿼리 통계(예: `UPDATE seats SET status = 'BOOKED' WHERE seat_id = $1 AND status = 'LOCKED' RETURNING seat_id, status`) 확인 가능
- **알려진 한계**: APM 트레이스 스팬에서 "Database Monitoring is not enabled" 표시 — dd-trace-py의 APM↔DBM 트레이스-쿼리 상관관계 기능은 Python 기준 `psycopg2`만 지원, Inventory가 쓰는 `asyncpg`는 미지원([DataDog/dd-trace-py#7966](https://github.com/DataDog/dd-trace-py/issues/7966)). DBM 자체(쿼리 통계)는 정상 동작하며, 드라이버 교체는 스코프 밖으로 판단해 스킵

#### 5.7 대시보드 및 알림

- 대시보드 위젯: 서비스별 CPU/메모리(5.1), 에러 로그 건수(5.2), 트레이스 에러율·레이턴시(5.3)
- Monitor 예시: Payment 에러율 임계치 초과, Reservation 종단 레이턴시 임계치 초과 — 5.3 데모에서 장애를 최초로 감지하는 트리거

---

## 6. 리소스 스펙

- Fargate Task CPU/메모리: 512 / 1024 MB (앱 128/256, Datadog Agent 256/512, FireLens 128/256)
- Desired count: 서비스당 1
- RDS: db.t3.micro, 20GB gp3, Single-AZ
- NAT Gateway: 1개
- ALB: 1개
- 리전: ap-northeast-2 (서울)

---

## 7. 장애 주입 메커니즘

- 라이브 데모 특성상 랜덤 장애가 아니라 즉시 토글 가능한 방식 채택. Task 재배포 방식(30초~1분 소요)은 발표 흐름을 끊으므로 배제.

#### 7.1 Payment — 주 장애 주입 지점

- `POST /admin/chaos` 엔드포인트 (in-memory 플래그, 재기동 불필요)
- Payment는 ALB에 안 붙어있어 외부에서 직접 호출 불가 — Gateway가 `POST /admin/chaos`를 그대로 프록시해서 노출(`PAYMENT_URL` env var로 Service Connect 경유 호출). 토글은 `scripts/chaos.sh <latency|error|off> [--delay-ms N] [--error-rate R]`로 실행(`off`가 곧 장애 해제)

```json
{ "mode": "latency" | "error" | "off", "delay_ms": 5000, "error_rate": 1.0 }
```

- `latency`: `/charge` 응답에 지정 지연 강제 추가 → Reservation 병렬 호출 중 하나가 느려지며 전체 예약 응답 지연/타임아웃
- `error`: 지정 비율로 500 강제 반환 → Reservation은 이를 409가 아닌 **502**로 변환 반환(순수 좌석 충돌과 다운스트림 장애 구분, 2.4 참고), 보상 트랜잭션(Inventory `/release`) 흐름까지 시연 가능. ddtrace가 5xx 자동 error 마킹 + Datadog Log Pipeline(`jy-project-409-as-error`)이 `http.status_code:>=500`을 error로 재분류해 APM/로그 양쪽에서 확인됨
- `off`: 정상 복귀

#### 7.2 Inventory — 보조 시나리오

기존 좌석 잠금 로직(409 Conflict)을 그대로 활용. 동일 좌석에 동시 예약 요청 2개를 보내면 자연 재현. 인프라 장애가 아닌 비즈니스 로직 충돌 시연용 — Log Pipeline에서 `status:warning`으로 분류되어 진짜 장애(error)와 구분됨.

#### 7.3 배경 트래픽 생성

`scripts/load-test.sh --rps N --duration N`으로 `GET /seats`/`POST /reservations`/취소를 섞은 트래픽을 반복 생성(순수 bash+curl, 별도 도구 불필요). 좌석 목록은 하드코딩 대신 실행 시점에 `/seats`에서 그대로 가져와서 좌석 구성이 바뀌어도 안전. `scripts/reset-seats.sh`로 테스트 후 좌석을 일괄 초기화 가능.

#### 7.4 주의사항

`/admin/chaos`는 인증 없이 열어둔 데모 전용 엔드포인트. 실제 프로덕션 패턴이 아님.

---

## 8. 정리

- 데모 종료 후 NAT Gateway와 ALB는 상시 과금 리소스. 데모 종료 후 ECS 서비스/클러스터, RDS, ALB, NAT Gateway, VPC 순으로 리소스 정리 필요
